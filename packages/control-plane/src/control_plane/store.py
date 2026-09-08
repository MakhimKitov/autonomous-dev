"""Platform-owned persistence: the session archive and the ledger.

The agent holds no storage credentials — all I/O here happens in the control
plane, out-of-band of the burst (ARCHITECTURE.md §6 "State & storage"). Two
stores, two protocols:

- ``SessionArchive`` — immutable transcript snapshots, one per burst, addressed by
  key. ``LocalSessionArchive`` here, ``ObjectSessionArchive`` (S3) in
  ``objectstore.py``; ``open_archive`` picks by the presence of ``ARCHIVE_URL``.
- ``Ledger`` — the queryable index (tasks, burst ledger, violations, events). Two
  backends behind one protocol: ``SqliteLedger`` here, ``PostgresLedger`` in
  ``postgres.py``; ``open_ledger`` picks by the presence of ``LEDGER_DSN``.

Every ledger write is one of STORES.md's four single-statement shapes — a lease
claim, a compare-and-set transition, a mutation with its outbox row, an
aggregate read — and no method leaves a transaction open when it returns
("transactional, but not interactive", E1). ``in_transaction`` on each backend is
the seam the test suite asserts that with, after every call and at every I/O
point of a tick.

Each pair is the same design with and without contention, never a mode flag, and
the two ``open_*`` functions are the only places a backend is chosen.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tarfile
from collections.abc import Callable
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import structlog

from control_plane.models import (
    BURST_FINALIZE_COLUMNS,
    EVENT_ORG_RESUMED,
    EVENT_ORG_STOPPED,
    EVENT_PRODUCT_PAUSED,
    EVENT_PRODUCT_REGISTERED,
    EVENT_PRODUCT_RESUMED,
    EVENT_PRODUCT_UPDATED,
    EVENT_PRODUCT_WOKEN,
    WAKE_SETTLE_S,
    EVENT_ROLE_UPDATED,
    EVENT_SUBSCRIPTION_EXPIRED,
    EVENT_SUBSCRIPTION_REGISTERED,
    EVENT_SUBSCRIPTION_REVOKED,
    EVENT_SUBSCRIPTION_UPDATED,
    EVENT_VIOLATION_RECORDED,
    ORG_TARGET,
    TASK_TRANSITION_COLUMNS,
    AuditRecord,
    BurstRecord,
    Event,
    OrgStop,
    ProductState,
    RepositoryRecord,
    RoleRecord,
    SubscriptionRecord,
    SubscriptionStatus,
    TaskRecord,
    Violation,
    utc_iso,
    utc_now,
)
from control_plane.roles import RoleError
from control_plane.roles import check_settable as check_role_settable
from control_plane.subscriptions import SubscriptionError, check_cap, check_ref
from control_plane.subscriptions import check_settable as check_subscription_settable

log = structlog.get_logger()

# The two presence toggles (STORES.md), and the only two. Set LEDGER_DSN and the
# ledger is a service database whose schema `control-plane migrate` owns; set
# ARCHIVE_URL and the archive is an S3 bucket. Unset, each is the single-node
# variant — a SQLite file and a local directory under the data dir. Neither is a
# mode flag: the same design, minus contention, and they toggle independently
# because a deployment can buy one capability before the other.
LEDGER_DSN_ENV = "LEDGER_DSN"
ARCHIVE_URL_ENV = "ARCHIVE_URL"


def session_key(task_id: str, burst_id: str) -> str:
    """Where a burst's snapshot lives (STORES.md, "The session archive").

    `sessions/{task_id}/{burst_id}/session.tar.gz` — one immutable object per
    burst. `task_id` becomes `owner/repo#n` at A6, which is legal in S3 keys and
    on POSIX; presigned URLs percent-encode the `#`.
    """
    return f"sessions/{task_id}/{burst_id}/session.tar.gz"


class SessionArchive(Protocol):
    """Dumb storage, addressed by key. The ledger owns the tree and the truth.

    Three properties every backend has to hold, because callers rely on them
    rather than on which backend they got:

    - **Write-once.** A `put` to an existing key raises. Snapshots never mutate;
      a retry is a new burst with a new key, and branching is a ledger write
      (`resumed_from_burst_id`), never an archive one.
    - **Checksummed against the ledger.** `get` takes the sha256 the ledger
      recorded and raises if the bytes disagree. The backend's own copy of that
      number is a convenience for looking at, never the authority.
    - **No credentials, ever.** `EXCLUDE_FROM_ARCHIVE` is applied on the way in,
      unconditionally.
    """

    def put(self, key: str, session_dir: Path) -> str:
        """Archive session_dir under key; returns its sha256. Write-once."""
        ...

    def get(self, key: str, dest_dir: Path, expected_sha256: str) -> None:
        """Materialize key into dest_dir. Missing key or checksum mismatch raises."""
        ...

    def recorded_checksum(self, key: str) -> str | None:
        """The sha256 the archive itself has stored beside the object, if any.

        Not the authority — the ledger is — and deliberately separate from `get`.
        It exists for the case where the control plane never held the bytes: with
        presigned PUT (G2, #43) the harvester uploads directly, and this is how
        the checksum gets back to the ledger.
        """
        ...


class PresigningArchive(SessionArchive, Protocol):
    """A backend that can lend out per-object access without lending credentials.

    The capability the three-container pod is built on (TO-BE §6, G2): the burst
    gets a URL for exactly one object, expiring inside its own rail, instead of a
    storage credential it could use for anything. `DockerRunner` never calls these
    — it keeps putting and getting in-process — which is why this is a separate
    protocol rather than two more methods every backend has to fake.
    """

    def presign_get(self, key: str, expires_in: int) -> str: ...
    def presign_put(self, key: str, expires_in: int) -> str: ...


# Credentials must never reach the archive. With CLAUDE_CONFIG_DIR set, the CLI
# stores .credentials.json under it — i.e. on the session volume we harvest.
# Env-token auth shouldn't write it, but the exclusion is unconditional.
# auth.json is codex's credential file (mounted outside the volume — defense only).
EXCLUDE_FROM_ARCHIVE = {".credentials.json", "auth.json"}


class ArchiveError(RuntimeError):
    """The archive was asked for something it must refuse: an existing key, a
    missing one, or bytes that don't match the checksum the ledger recorded."""


class RegistryError(RuntimeError):
    """A registry verb the ledger must refuse: registering a product twice,
    mutating one that isn't registered, pausing without a reason, a no-op state
    change, or `set` reaching for a column that isn't the operator's to set."""


# What `product set` may touch — the policy and quota columns, exactly.
# `state` is deliberately absent: pause/resume own it, with pause's reason
# discipline; `last_burst_at` and the claim columns are the scheduler's, and the
# key and timestamps are nobody's. Both backends check against this one tuple.
PRODUCT_SET_COLUMNS = (
    "team",
    "review_enabled",
    "review_provider",
    "e2e_enabled",
    "test_env_refs",
    "concurrency",
    "daily_bursts",
    "usage_share",
)


def _registry_transition(state: str, reason: str) -> tuple[str, str]:
    """The shared gate for pause/resume: the audit action and event kind for a
    state, with TO-BE §2's rule — pausing requires a reason — enforced in the
    one place both backends pass through."""
    if state == ProductState.PAUSED:
        if not reason.strip():
            raise RegistryError("pausing a product requires a reason — it lands in the audit row")
        return "product.pause", EVENT_PRODUCT_PAUSED
    if state == ProductState.ACTIVE:
        return "product.resume", EVENT_PRODUCT_RESUMED
    raise RegistryError(f"a product is active or paused — {state!r} is neither")


def _pool_transition(status: str, reason: str) -> tuple[str, str]:
    """The shared gate for the pool's endings (F1), the twin of
    `_registry_transition`: the audit action and event kind for a status, and
    the one rule — revoking a member's contribution says why. There is no
    transition *to* `active`: registration is the only way in, and coming back
    after an ending is renewal (F4), which re-materializes at the same ref."""
    if status == SubscriptionStatus.REVOKED:
        if not reason.strip():
            raise SubscriptionError("revoking a subscription requires a reason — it lands in the audit row")
        return "subscription.revoke", EVENT_SUBSCRIPTION_REVOKED
    if status == SubscriptionStatus.EXPIRED:
        return "subscription.expire", EVENT_SUBSCRIPTION_EXPIRED
    raise SubscriptionError(
        f"a subscription ends as revoked or expired — {status!r} is neither"
    )


def _pool_refusal(existing: SubscriptionRecord | None, subscription_id: str, status: str) -> str:
    """The CAS refused and nothing was written, so read the row to say why."""
    if existing is None:
        return f"{subscription_id} is not a registered subscription"
    return f"{subscription_id} is already {status}"


def _already_contributed(subscription_id: str, *, check: bool) -> str:
    """Why a pool write bounced: the member already contributes, or the schema's
    own wall caught a row the grammar should have stopped.

    The driver's message is deliberately not passed through. Postgres puts the
    *failing row* in a CHECK violation's DETAIL — which, for this table, is the
    value someone tried to store as a ref — and this string reaches a terminal
    and a log. What no column may hold, no error message may repeat."""
    if check:
        return (
            "the schema refused this row — `secret_ref` is env:NAME, file:PATH or helper:NAME, "
            "and `cap_tokens` is positive"
        )
    return (
        f"{subscription_id} already contributes a subscription — `subscription set` changes "
        f"its cap, ref or expiry; `subscription revoke` retires it"
    )


def _check_stop_reason(reason: str) -> None:
    """The org STOP's one rule (E3), in the one place both backends pass
    through: a stop names why. The reason is what every replica logs while it
    stands down and what `resume` is read against later — an empty one would
    leave the whole fleet idle with nothing to say for it."""
    if not reason.strip():
        raise RegistryError("stopping the org requires a reason — it lands in the audit row and in every replica's log")


def _already_stopped(stop: OrgStop | None) -> str:
    """The refusal a second `stop` earns — and the answer to "is it stopped?",
    since the message names the stop in force."""
    if stop is None:  # the CAS refused, yet the row reads running: a `resume` raced in between
        return "the org was stopped when this call arrived and has been resumed since — retry"
    return f"the org is already stopped — since {stop.stopped_at} by {stop.stopped_by}: {stop.reason}"


def _check_settable(changes: dict) -> None:
    if not changes:
        raise RegistryError("nothing to set — name at least one column to change")
    unknown = sorted(set(changes) - set(PRODUCT_SET_COLUMNS))
    if unknown:
        raise RegistryError(
            f"not settable: {', '.join(unknown)} — `product set` touches only "
            f"{', '.join(PRODUCT_SET_COLUMNS)}"
        )


def _check_transition(changes: dict) -> None:
    """A transition names the columns it changes and nothing else — the key and
    `created_at` are not the scheduler's to move, and an unknown name is a typo
    that would otherwise bind nothing and report success."""
    if not changes:
        raise ValueError("a transition changes at least one column")
    unknown = sorted(set(changes) - set(TASK_TRANSITION_COLUMNS))
    if unknown:
        raise ValueError(
            f"not a transition column: {', '.join(unknown)} — transitions write only "
            f"{', '.join(TASK_TRANSITION_COLUMNS)}"
        )


def _after(now_iso: str, seconds: float) -> str:
    """`now_iso` + `seconds`, in the spelling every stored stamp has. The SQLite
    backend's lease arithmetic: one process, so its clock is the ledger's."""
    return (datetime.fromisoformat(now_iso) + timedelta(seconds=seconds)).isoformat()


def tar_without_credentials(session_dir: Path, destination: Path) -> str:
    """Tar session_dir to destination, minus credential files. Returns sha256."""

    def _skip_credentials(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if Path(info.name).name in EXCLUDE_FROM_ARCHIVE else info

    with tarfile.open(destination, "w:gz") as tar:
        tar.add(session_dir, arcname=".", filter=_skip_credentials)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def untar(blob: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(blob, "r:gz") as tar:
        tar.extractall(dest_dir, filter="data")


class LocalSessionArchive:
    """The single-node archive: the key is a path under a local root.

    Same three properties as the object backend, in the cheap place — write-once
    is a file that already exists, and the checksum sidecar is what
    `recorded_checksum` reads where an object store would read metadata.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _blob(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, session_dir: Path) -> str:
        blob = self._blob(key)
        if blob.exists():
            raise ArchiveError(f"snapshots are write-once and {key} already exists")
        blob.parent.mkdir(parents=True, exist_ok=True)
        checksum = tar_without_credentials(session_dir, blob)
        blob.with_suffix(blob.suffix + ".sha256").write_text(checksum, encoding="utf-8")
        return checksum

    def get(self, key: str, dest_dir: Path, expected_sha256: str) -> None:
        blob = self._blob(key)
        if not blob.is_file():
            raise ArchiveError(f"no snapshot at {key}")
        actual = hashlib.sha256(blob.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise ArchiveError(
                f"checksum mismatch for {key}: ledger says {expected_sha256}, bytes say {actual}"
            )
        untar(blob, dest_dir)

    def recorded_checksum(self, key: str) -> str | None:
        blob = self._blob(key)
        sidecar = blob.with_suffix(blob.suffix + ".sha256")
        return sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else None


class Ledger(Protocol):
    def register_repository(self, repo: RepositoryRecord, actor: str, reason: str = "") -> None: ...
    def get_repository(self, repository: str) -> RepositoryRecord | None: ...
    def list_repositories(self) -> list[RepositoryRecord]: ...
    def set_repository_state(self, repository: str, state: str, actor: str, reason: str = "") -> None: ...
    def update_repository(self, repository: str, changes: dict, actor: str, reason: str = "") -> None: ...
    def touch_repository_burst(self, repository: str, at: str) -> None: ...
    def claim_observation(self, repository: str, replica: str, lease_s: float) -> bool: ...
    def wake_repository(
        self, repository: str, kind: str, delivery: str | None = None, *, settle_s: float = WAKE_SETTLE_S
    ) -> bool: ...
    def subscribe_wakes(self, on_wake: Callable[[str], None]) -> None: ...
    def org_stop(self) -> OrgStop | None: ...
    def stop_org(self, actor: str, reason: str) -> None: ...
    def resume_org(self, actor: str, reason: str = "") -> None: ...
    def audit_trail(self, target: str | None = None) -> list[AuditRecord]: ...
    def list_roles(self) -> list[RoleRecord]: ...
    def get_role(self, role: str) -> RoleRecord | None: ...
    def update_role(self, role: str, changes: dict, actor: str, reason: str = "") -> None: ...
    def register_subscription(self, sub: SubscriptionRecord, actor: str, reason: str = "") -> None: ...
    def get_subscription(self, subscription_id: str) -> SubscriptionRecord | None: ...
    def list_subscriptions(self) -> list[SubscriptionRecord]: ...
    def set_subscription_status(
        self, subscription_id: str, status: str, actor: str, reason: str = ""
    ) -> None: ...
    def update_subscription(
        self, subscription_id: str, changes: dict, actor: str, reason: str = ""
    ) -> None: ...
    def upsert_task(self, task: TaskRecord) -> None: ...
    def create_task(self, task: TaskRecord) -> bool: ...
    def transition_task(
        self, task_id: str, changes: dict, *, expected_status: str, expected_holder: str | None
    ) -> TaskRecord | None: ...
    def claim_next(
        self,
        repository: str,
        replica: str,
        *,
        ceiling: int,
        since: str,
        lease_s: float,
        cooldown_s: float,
        concurrency: int,
    ) -> TaskRecord | None: ...
    def renew_lease(self, task_id: str, replica: str, lease_s: float) -> bool: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def tasks_by_status(
        self, *statuses: str, repository: str | None = None, orphaned: bool = False
    ) -> list[TaskRecord]: ...
    def record_burst(self, burst: BurstRecord) -> None: ...
    def finish_burst(self, burst: BurstRecord) -> None: ...
    def get_burst(self, burst_id: str) -> BurstRecord | None: ...
    def latest_archived_burst(self, task_id: str) -> BurstRecord | None: ...
    def running_bursts(
        self, repository: str | None = None, *, orphaned: bool = False
    ) -> list[BurstRecord]: ...
    def bursts_started_since(self, since_iso: str, repository: str | None = None) -> int: ...
    def record_violation(self, violation: Violation) -> None: ...
    def violations(self, task_id: str | None = None) -> list[Violation]: ...
    def events(self, after_id: int = 0) -> list[Event]: ...
    def close(self) -> None: ...


LEDGER_FILENAME = "ledger.sqlite3"


def open_ledger(data_dir: Path) -> Ledger:
    """The ledger this deployment has: Postgres if ``LEDGER_DSN`` is set, else the
    SQLite file under ``data_dir``. The one place that choice is made."""
    dsn = os.environ.get(LEDGER_DSN_ENV, "").strip()
    if not dsn:
        return SqliteLedger(data_dir / LEDGER_FILENAME)
    from control_plane.postgres import PostgresLedger  # local: keeps the seam one-way

    log.info("ledger backend is postgres", dsn=_redact(dsn))
    return PostgresLedger(dsn)


def open_archive(data_dir: Path) -> SessionArchive:
    """The archive this deployment has: an S3 bucket if ``ARCHIVE_URL`` is set,
    else a directory under ``data_dir``. `open_ledger`'s twin, deliberately — one
    presence toggle each, one string each, and nothing else in the codebase
    allowed to name a backend."""
    url = os.environ.get(ARCHIVE_URL_ENV, "").strip()
    if not url:
        # rooted at the data dir, not at data/sessions: every key already begins
        # `sessions/`, so the local tree and the bucket lay out identically and a
        # backfill or an eyeball can compare them path for path
        return LocalSessionArchive(data_dir)
    from control_plane.objectstore import ObjectSessionArchive  # local: one-way seam

    log.info("archive backend is an object store", url=_redact(url))
    return ObjectSessionArchive.from_url(url)


def _redact(dsn: str) -> str:
    """A DSN is loggable; the password inside it is not.

    Both forms psycopg accepts: the URL's ``//user:pass@host`` (a password must
    percent-encode ``@``, so matching to the last one is safe) and the key=value
    conninfo's ``password=``.
    """
    url_form = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", dsn)
    return re.sub(r"(password\s*=\s*)\S+", r"\1***", url_form)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    repository TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    state TEXT NOT NULL,
    review_enabled INTEGER NOT NULL,
    review_provider TEXT NOT NULL,
    e2e_enabled INTEGER NOT NULL,
    test_env_refs TEXT NOT NULL DEFAULT '{}',
    concurrency INTEGER NOT NULL,
    daily_bursts INTEGER NOT NULL,
    usage_share REAL NOT NULL,
    last_burst_at TEXT,
    claimed_by TEXT,
    claimed_until TEXT,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    role TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('pipeline', 'custom')),
    prompt_preamble TEXT NOT NULL,
    tools TEXT NOT NULL,
    model TEXT NOT NULL,
    max_budget_usd REAL NOT NULL,
    max_turns INTEGER NOT NULL,
    mount_mode TEXT NOT NULL,
    identity TEXT NOT NULL CHECK (identity IN ('none', 'automation')),
    sink TEXT NOT NULL CHECK (sink IN ('issue_comment', 'ledger_artifact', 'event')),
    trigger TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- the four trust slots, seeded exactly as migration 0002 seeds them (D5 reads
-- them; an existing file gains them on open — OR IGNORE keeps operator edits)
INSERT OR IGNORE INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, max_turns,
                             mount_mode, identity, sink, created_at, updated_at)
VALUES
    ('dev',      'pipeline', '', '["*"]', 'claude-sonnet-5', 2.0, 100, 'rw', 'none', 'issue_comment', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('reviewer', 'pipeline', '', '["*"]', 'claude-sonnet-5', 2.0, 100, 'ro', 'none', 'issue_comment', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('tester',   'pipeline', '', '["*"]', 'claude-sonnet-5', 2.0, 100, 'ro', 'none', 'issue_comment', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
    ('monitor',  'pipeline', '', '["*"]', 'claude-sonnet-5', 2.0, 100, 'ro', 'none', 'event', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
-- WALL: pipeline rows are frozen — the SQLite twin of roles_pipeline_guard
-- (0002). RAISE(ABORT) surfaces as sqlite3.IntegrityError, the same class a
-- constraint gives, so callers see one kind of refusal on both backends.
CREATE TRIGGER IF NOT EXISTS roles_pipeline_guard_update
    BEFORE UPDATE ON roles FOR EACH ROW
    WHEN OLD.kind = 'pipeline' AND (
        NEW.role IS NOT OLD.role OR NEW.kind IS NOT OLD.kind OR NEW.identity IS NOT OLD.identity
        OR NEW.sink IS NOT OLD.sink OR NEW.tools IS NOT OLD.tools)
BEGIN
    SELECT RAISE(ABORT, 'seeded pipeline role: role, kind, identity, sink and tools are frozen');
END;
CREATE TRIGGER IF NOT EXISTS roles_pipeline_guard_delete
    BEFORE DELETE ON roles FOR EACH ROW
    WHEN OLD.kind = 'pipeline'
BEGIN
    SELECT RAISE(ABORT, 'seeded pipeline role cannot be deleted');
END;
-- subscriptions — the ref-only pool (F1), the SQLite twin of migration 0017.
-- `secret_ref` carries the ingestion wall in the schema itself: the three
-- schemes the resolver speaks, each followed by a name. SQLite has no regex, so
-- the twin of Postgres's `~ '^(env|file|helper):.'` is spelled in GLOB — `?*` is
-- "one character, then anything", and GLOB is the case-sensitive one (LIKE would
-- take `ENV:` too, and then the two backends would not mean the same thing).
CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    member TEXT NOT NULL,
    secret_ref TEXT NOT NULL CHECK (
        secret_ref GLOB 'env:?*' OR secret_ref GLOB 'file:?*' OR secret_ref GLOB 'helper:?*'
    ),
    cap_tokens INTEGER NOT NULL CHECK (cap_tokens > 0),
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'expired', 'revoked')),
    backoff_until TEXT,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
-- org_state — exactly one row, the SQLite twin of migration 0016 (E3): the key
-- must be 1, so a second row is a constraint violation; the stop columns are
-- all-or-nothing. Seeded running on open (OR IGNORE keeps a stop in force).
CREATE TABLE IF NOT EXISTS org_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    stop_reason TEXT,
    stopped_by TEXT,
    stopped_at TEXT,
    CHECK ((stop_reason IS NULL) = (stopped_by IS NULL) AND (stop_reason IS NULL) = (stopped_at IS NULL))
);
INSERT OR IGNORE INTO org_state (id) VALUES (1);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    pr_number INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    review_rounds INTEGER NOT NULL DEFAULT 0,
    next_burst_kind TEXT,
    last_review_id INTEGER,
    last_burst_at TEXT,
    claimed_by TEXT,
    claimed_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bursts (
    burst_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT '',
    resumed_from_burst_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    model TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    num_turns INTEGER,
    session_id TEXT,
    session_key TEXT,
    session_checksum TEXT
);
CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    burst_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class LedgerShapeError(RuntimeError):
    """This SQLite ledger predates the derived task id and cannot be read as-is."""


# Columns added to a *current-shape* file after it was created. `CREATE TABLE IF NOT
# EXISTS` does nothing to a table that already exists, so a file made before the
# column would fail the next insert.
#
# This is not the ALTER list A6 deleted. That one existed to repair files whose task
# ids could not be repaired at all — an id needs a repository slug the file doesn't
# contain, so adding columns was guesswork dressed as migration, and refusing was
# the honest answer. A column with a default and no history to reconstruct is a
# different thing: `span_id` is empty for every burst that ran before B1, which is
# exactly the truth about those bursts.
#
# The Postgres schema stays authoritative (STORES.md) — an entry here is the SQLite
# half of a numbered migration, never a schema change of its own.
_ADDITIVE_COLUMNS = (
    ("bursts", "span_id", "TEXT NOT NULL DEFAULT ''"),  # 0009, B1 (#19)
    ("tasks", "claimed_by", "TEXT"),  # 0015, E1 (#32) — NULL: nobody holds the task
    ("tasks", "claimed_until", "TEXT"),
)


class SqliteLedger:
    """The single-node ledger: stdlib SQLite, the same protocol Postgres answers.

    It self-bootstraps — `_SCHEMA` is `CREATE TABLE IF NOT EXISTS`, so a fresh file
    is ready on open and no runner is involved. What it no longer does is *migrate*.
    Until A6 this class carried a list of additive `ALTER TABLE … ADD COLUMN`
    repairs, one per column the MVP grew; A6 retires this ledger as an origin
    (STORES.md, "From the MVP"), and every file that list existed for is a file
    whose task ids predate `{owner}/{repo}#{n}`. Those cannot be repaired by adding
    a column — deriving an id needs a repository slug that is nowhere in the file —
    so the honest move is to refuse, name the one-shot that *can* do it, and delete
    the guesswork. `_ADDITIVE_COLUMNS` is the narrow case that survived: a column
    with a default, on a file this class can still read.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the wake endpoint (E5) holds a handle of its
        # own and writes through it from the server's threads, serialized by its
        # lock. The guard removed here never protected a transaction — the
        # module is compiled serialized — only a connection from being touched
        # off its thread at all; the loop's handle stays the loop's regardless.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)  # a no-op on a file that already has them
        self._require_current_shape(path)
        self._add_columns_a_current_file_may_lack()
        self._conn.commit()

    def _require_current_shape(self, path: Path) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
        missing = {"repository", "issue_number"} - columns
        if not missing:
            return
        self._conn.close()
        raise LedgerShapeError(
            f"{path} predates the derived task id (missing {', '.join(sorted(missing))}). "
            "Its rows are keyed by a bare issue number, which no longer identifies a "
            "task. Move them with `control-plane backfill --repository owner/name "
            f"--data-dir {path.parent}`, which reads this file without writing to it, "
            "or point --data-dir at a fresh directory to start clean."
        )

    def _add_columns_a_current_file_may_lack(self) -> None:
        """Runs after the refusal above, so it only ever touches a file worth
        keeping. One statement per missing column and nothing else — no version
        table, because `PRAGMA table_info` already knows the answer."""
        for table, column, definition in _ADDITIVE_COLUMNS:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # --- the product registry (C1) ---
    #
    # Every mutation writes its audit row and its outbox event in the same
    # transaction — the SQLite spelling of the Postgres backend's single
    # data-modifying CTE. The guarantee callers get is identical: no mutation
    # without its audit row, no audit row without its announcement.

    def _audit_and_announce(self, actor: str, action: str, target: str, reason: str,
                            kind: str, extra: dict | None = None, subject: str | None = "repository") -> None:
        """The two rows every registry mutation trails. Caller commits.
        ``subject`` is the payload key the target rides under — a product's
        events say ``repository``, a role's say ``role`` (D5); None for the org
        verbs (E3), whose target is the org itself and rides under no key."""
        cursor = self._conn.execute(
            "INSERT INTO audit (actor, action, target, reason, at) VALUES (?, ?, ?, ?, ?)",
            (actor, action, target, reason, utc_now()),
        )
        payload = {"audit_id": cursor.lastrowid, "actor": actor, **(extra or {})}
        if subject is not None:
            payload[subject] = target
        self._conn.execute(
            "INSERT INTO events (kind, payload, created_at) VALUES (?, ?, ?)",
            (kind, json.dumps(payload), utc_now()),
        )

    def register_repository(self, repo: RepositoryRecord, actor: str, reason: str = "") -> None:
        data = asdict(repo)
        data["test_env_refs"] = json.dumps(data["test_env_refs"])
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        try:
            self._conn.execute(f"INSERT INTO repositories ({cols}) VALUES ({placeholders})", data)
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise RegistryError(f"{repo.repository} is already registered") from exc
        self._audit_and_announce(actor, "product.register", repo.repository, reason,
                                 EVENT_PRODUCT_REGISTERED)
        self._conn.commit()

    def _load_repository(self, row: sqlite3.Row) -> RepositoryRecord:
        data = dict(row)
        data["review_enabled"] = bool(data["review_enabled"])
        data["e2e_enabled"] = bool(data["e2e_enabled"])
        data["test_env_refs"] = json.loads(data["test_env_refs"])
        return RepositoryRecord(**data)

    def get_repository(self, repository: str) -> RepositoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM repositories WHERE repository = ?", (repository,)
        ).fetchone()
        return self._load_repository(row) if row else None

    def list_repositories(self) -> list[RepositoryRecord]:
        rows = self._conn.execute("SELECT * FROM repositories ORDER BY repository").fetchall()
        return [self._load_repository(row) for row in rows]

    def set_repository_state(self, repository: str, state: str, actor: str, reason: str = "") -> None:
        action, kind = _registry_transition(state, reason)
        cursor = self._conn.execute(
            "UPDATE repositories SET state = ?, updated_at = ? WHERE repository = ? AND state <> ?",
            (state, utc_now(), repository, state),
        )
        if cursor.rowcount == 0:
            # the CAS refused; nothing was written, so read to say why
            self._conn.rollback()
            existing = self.get_repository(repository)
            if existing is None:
                raise RegistryError(f"{repository} is not registered")
            raise RegistryError(f"{repository} is already {state}")
        self._audit_and_announce(actor, action, repository, reason, kind)
        self._conn.commit()

    def update_repository(self, repository: str, changes: dict, actor: str, reason: str = "") -> None:
        _check_settable(changes)
        values = dict(changes)
        if "test_env_refs" in values:
            values["test_env_refs"] = json.dumps(values["test_env_refs"])
        # column names come from PRODUCT_SET_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = :{column}" for column in values)
        cursor = self._conn.execute(
            f"UPDATE repositories SET {assignments}, updated_at = :updated_at "
            f"WHERE repository = :repository",
            {**values, "updated_at": utc_now(), "repository": repository},
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise RegistryError(f"{repository} is not registered")
        self._audit_and_announce(actor, "product.set", repository, reason,
                                 EVENT_PRODUCT_UPDATED, extra={"changed": sorted(changes)})
        self._conn.commit()

    def audit_trail(self, target: str | None = None) -> list[AuditRecord]:
        select = "SELECT id, actor, action, target, reason, at FROM audit"
        if target is None:
            rows = self._conn.execute(f"{select} ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(f"{select} WHERE target = ? ORDER BY id", (target,)).fetchall()
        return [AuditRecord(**dict(row)) for row in rows]

    # --- the subscription pool (F1) ---
    #
    # The registry's discipline on the pool: every verb is one statement with
    # its audit row and its announcement, and what it stores is a ref. Nothing
    # here can hold a token — `check_ref` refuses a value, and the table's own
    # CHECK refuses one that got past the check.

    @staticmethod
    def _load_subscription(row: sqlite3.Row) -> SubscriptionRecord:
        return SubscriptionRecord(**dict(row))

    def register_subscription(self, sub: SubscriptionRecord, actor: str, reason: str = "") -> None:
        data = asdict(sub)
        check_ref(data["secret_ref"])
        check_cap(data["cap_tokens"])
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        try:
            self._conn.execute(f"INSERT INTO subscriptions ({cols}) VALUES ({placeholders})", data)
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            check = "CHECK" in str(exc).upper()
            # `from None`: a chained cause prints too, and a CHECK violation's text
            # is about the row that was refused
            raise SubscriptionError(_already_contributed(sub.subscription_id, check=check)) from None
        self._audit_and_announce(actor, "subscription.register", sub.subscription_id, reason,
                                 EVENT_SUBSCRIPTION_REGISTERED, subject="subscription")
        self._conn.commit()

    def get_subscription(self, subscription_id: str) -> SubscriptionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
        ).fetchone()
        return self._load_subscription(row) if row else None

    def list_subscriptions(self) -> list[SubscriptionRecord]:
        rows = self._conn.execute("SELECT * FROM subscriptions ORDER BY subscription_id").fetchall()
        return [self._load_subscription(row) for row in rows]

    def set_subscription_status(self, subscription_id: str, status: str, actor: str, reason: str = "") -> None:
        action, kind = _pool_transition(status, reason)
        cursor = self._conn.execute(
            "UPDATE subscriptions SET status = ? WHERE subscription_id = ? AND status <> ?",
            (status, subscription_id, status),
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise SubscriptionError(_pool_refusal(self.get_subscription(subscription_id), subscription_id, status))
        self._audit_and_announce(actor, action, subscription_id, reason, kind, subject="subscription")
        self._conn.commit()

    def update_subscription(self, subscription_id: str, changes: dict, actor: str, reason: str = "") -> None:
        check_subscription_settable(changes)
        if "secret_ref" in changes:
            check_ref(changes["secret_ref"])
        if "cap_tokens" in changes:
            check_cap(changes["cap_tokens"])
        # column names come from SUBSCRIPTION_SET_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        try:
            cursor = self._conn.execute(
                f"UPDATE subscriptions SET {assignments} WHERE subscription_id = :subscription_id",
                {**changes, "subscription_id": subscription_id},
            )
        except sqlite3.IntegrityError:  # the table's own wall, behind the grammar
            self._conn.rollback()
            raise SubscriptionError(_already_contributed(subscription_id, check=True)) from None
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise SubscriptionError(f"{subscription_id} is not a registered subscription")
        self._audit_and_announce(actor, "subscription.set", subscription_id, reason,
                                 EVENT_SUBSCRIPTION_UPDATED, extra={"changed": sorted(changes)},
                                 subject="subscription")
        self._conn.commit()

    # --- roles (D5) ---
    #
    # Read at every spawn, written by `role set` alone. The open fields are
    # checked by name in roles.check_settable before any SQL; the wall fields
    # are refused there and, beneath, by the schema's trigger — a bypass of the
    # first meets the second as an IntegrityError.

    @staticmethod
    def _load_role(row: sqlite3.Row) -> RoleRecord:
        data = dict(row)
        data["tools"] = json.loads(data["tools"])
        data["trigger"] = json.loads(data["trigger"]) if data["trigger"] else None
        return RoleRecord(**data)

    def list_roles(self) -> list[RoleRecord]:
        rows = self._conn.execute("SELECT * FROM roles ORDER BY kind DESC, role").fetchall()
        return [self._load_role(row) for row in rows]

    def get_role(self, role: str) -> RoleRecord | None:
        row = self._conn.execute("SELECT * FROM roles WHERE role = ?", (role,)).fetchone()
        return self._load_role(row) if row else None

    def update_role(self, role: str, changes: dict, actor: str, reason: str = "") -> None:
        check_role_settable(changes)
        # column names come from ROLE_SET_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        try:
            cursor = self._conn.execute(
                f"UPDATE roles SET {assignments}, updated_at = :updated_at WHERE role = :role",
                {**changes, "updated_at": utc_now(), "role": role},
            )
        except sqlite3.IntegrityError as exc:  # the trigger — a wall field got through
            self._conn.rollback()
            raise RoleError(f"{role}: {exc}") from exc
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise RoleError(f"no role named {role!r}")
        self._audit_and_announce(actor, "role.set", role, reason, EVENT_ROLE_UPDATED,
                                 extra={"changed": sorted(changes)}, subject="role")
        self._conn.commit()

    # --- tasks ---
    #
    # Two kinds of write (E1). `upsert_task` is the whole-row write for a row
    # nobody contends for — the seed path, the backfill, tests. Everything the
    # scheduler does is a compare-and-set: `create_task` inserts only if absent,
    # `transition_task` writes only the named columns and only if the row still
    # reads the (status, holder) the caller saw, `claim_next` and `renew_lease`
    # write the lease. One process cannot race itself, so on this backend the
    # guards are never what refuses — they are here because the two backends have
    # to answer identically (A7), settled in the cheap place before Postgres makes
    # them load-bearing (E2).

    def upsert_task(self, task: TaskRecord) -> None:
        task.updated_at = utc_now()
        data = asdict(task)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k not in ("task_id", "created_at"))
        self._conn.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(task_id) DO UPDATE SET {updates}",
            data,
        )
        self._conn.commit()

    def create_task(self, task: TaskRecord) -> bool:
        """Insert the row if the ledger has never seen the task; False when it
        has — untouched, whatever state it is in. Two replicas claiming one
        issue compute the same id, and the second learns it lost here."""
        data = asdict(task)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        cursor = self._conn.execute(
            f"INSERT OR IGNORE INTO tasks ({cols}) VALUES ({placeholders})", data
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def transition_task(
        self, task_id: str, changes: dict, *, expected_status: str, expected_holder: str | None
    ) -> TaskRecord | None:
        """Compare-and-set (STORES.md, "Transition"): write `changes` if the row
        still reads (`expected_status`, `expected_holder`); the row as written,
        or None when it had moved — the caller lost the race and writes nothing.
        `IS` is SQLite's null-safe equality: an unleased row's holder is NULL."""
        _check_transition(changes)
        # column names come from TASK_TRANSITION_COLUMNS (checked above), never the caller
        assignments = ", ".join(f"{column} = :{column}" for column in changes)
        row = self._conn.execute(
            f"UPDATE tasks SET {assignments}, updated_at = :_updated_at "
            f"WHERE task_id = :_task_id AND status = :_status AND claimed_by IS :_holder "
            f"RETURNING *",
            {**changes, "_updated_at": utc_now(), "_task_id": task_id,
             "_status": expected_status, "_holder": expected_holder},
        ).fetchone()
        self._conn.commit()
        return TaskRecord(**dict(row)) if row else None

    def claim_next(
        self,
        repository: str,
        replica: str,
        *,
        ceiling: int,
        since: str,
        lease_s: float,
        cooldown_s: float,
        concurrency: int,
    ) -> TaskRecord | None:
        """Admission (TO-BE §5): the rails, then the claim — the oldest
        `claimed` task of the product whose lease nobody holds and whose failure
        cooldown has passed, leased to `replica` for `lease_s` in the statement
        that picks it (STORES.md, "Claim — a lease is data"). Postgres fences the
        two under an advisory lock; one process needs no fence.

        What the rails count is *admitted* work, not recorded bursts. The daily
        ceiling: bursts started since `since`, plus spawns in flight — a live
        lease with no running burst row yet. The product's concurrency (E2): the
        tasks in flight right now — a live lease or a running burst, whichever
        stage the spawn is at. The in-flight terms are what keep two replicas
        from each seeing "one under" and both spawning; their one imprecision is
        conservative (a burst that has just finished but whose task has not yet
        moved counts for a few seconds more — a spawn deferred a tick, never one
        over the rail)."""
        now = utc_now()
        counts = self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM bursts b JOIN tasks t ON t.task_id = b.task_id"
            "        WHERE t.repository = :repository AND b.started_at >= :since)"
            "     + (SELECT COUNT(*) FROM tasks t"
            "        WHERE t.repository = :repository AND t.claimed_until > :now"
            "          AND NOT EXISTS (SELECT 1 FROM bursts b"
            "                          WHERE b.task_id = t.task_id AND b.status = 'running')) AS admitted,"
            "       (SELECT COUNT(*) FROM tasks t"
            "        WHERE t.repository = :repository"
            "          AND (t.claimed_until > :now"
            "               OR EXISTS (SELECT 1 FROM bursts b"
            "                          WHERE b.task_id = t.task_id AND b.status = 'running'))) AS in_flight",
            {"repository": repository, "since": utc_iso(since), "now": now},
        ).fetchone()
        if counts["admitted"] >= ceiling:
            log.warning("daily burst ceiling reached — not spawning", repo=repository, ceiling=ceiling)
            return None
        if counts["in_flight"] >= concurrency:
            log.info("product at its concurrency — not spawning", repo=repository, concurrency=concurrency)
            return None
        row = self._conn.execute(
            "UPDATE tasks SET claimed_by = :replica, claimed_until = :until "
            "WHERE task_id = ("
            "    SELECT task_id FROM tasks"
            "    WHERE repository = :repository AND status = 'claimed'"
            "      AND (claimed_until IS NULL OR claimed_until < :now)"
            "      AND (attempts = 0 OR last_burst_at IS NULL OR last_burst_at <= :cooled)"
            "    ORDER BY created_at, task_id LIMIT 1"
            ") RETURNING *",
            {"replica": replica, "until": _after(now, lease_s), "repository": repository,
             "now": now, "cooled": _after(now, -cooldown_s)},
        ).fetchone()
        self._conn.commit()
        return TaskRecord(**dict(row)) if row else None

    def renew_lease(self, task_id: str, replica: str, lease_s: float) -> bool:
        """The heartbeat: push the lease out by `lease_s`, only while `replica`
        still holds it. False means a peer has taken the task — the caller's
        work is no longer wanted and it fences itself off."""
        cursor = self._conn.execute(
            "UPDATE tasks SET claimed_until = :until WHERE task_id = :task_id AND claimed_by = :replica",
            {"until": _after(utc_now(), lease_s), "task_id": task_id, "replica": replica},
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return TaskRecord(**dict(row)) if row else None

    def tasks_by_status(
        self, *statuses: str, repository: str | None = None, orphaned: bool = False
    ) -> list[TaskRecord]:
        # `task_id` breaks the tie: the scheduler takes the first row as "oldest
        # first", and two tasks created in the same microsecond would otherwise be
        # ordered by whatever the storage happened to return (gate A7). The
        # repository scope is C4: a product's scheduler sees only its own tasks.
        # `orphaned` (E1) keeps only rows no live lease covers — what a dead
        # replica leaves behind, and what the reconciler may touch.
        marks = ", ".join("?" for _ in statuses)
        query = f"SELECT * FROM tasks WHERE status IN ({marks})"
        params: list = list(statuses)
        if repository is not None:
            query += " AND repository = ?"
            params.append(repository)
        if orphaned:
            query += " AND (claimed_until IS NULL OR claimed_until < ?)"
            params.append(utc_now())
        rows = self._conn.execute(query + " ORDER BY created_at, task_id", params).fetchall()
        return [TaskRecord(**dict(row)) for row in rows]

    def record_burst(self, burst: BurstRecord) -> None:
        data = asdict(burst)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        self._conn.execute(f"INSERT INTO bursts ({cols}) VALUES ({placeholders})", data)
        self._conn.commit()

    def finish_burst(self, burst: BurstRecord) -> None:
        """Finalize exactly once, by the same CAS the Postgres backend uses.

        One process can't race itself, so this guard buys nothing here today —
        it exists because the two backends have to be indistinguishable to
        callers (gate A7), and because the semantics should be settled in the
        cheap place before E1 makes them load-bearing.
        """
        assignments = ", ".join(f"{column}=:{column}" for column in BURST_FINALIZE_COLUMNS)
        cursor = self._conn.execute(
            f"UPDATE bursts SET {assignments} WHERE burst_id=:burst_id AND status='running'",
            asdict(burst),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            log.warning(
                "burst already finalized — result not overwritten",
                burst=burst.burst_id,
                attempted_status=burst.status,
            )

    def get_burst(self, burst_id: str) -> BurstRecord | None:
        row = self._conn.execute(
            "SELECT * FROM bursts WHERE burst_id = ?", (burst_id,)
        ).fetchone()
        return BurstRecord(**dict(row)) if row else None

    def latest_archived_burst(self, task_id: str) -> BurstRecord | None:
        """The leaf of this task's session tree: the newest *dev* burst that
        archived a snapshot. What the next burst's volume is materialized from,
        and what its `resumed_from_burst_id` points at. The role filter is what
        makes a tester's archived snapshot evidence rather than memory (B5):
        without it, one archived tester burst and the next dev round would
        resume the tester's session."""
        row = self._conn.execute(
            "SELECT * FROM bursts WHERE task_id = ? AND session_key IS NOT NULL "
            "AND role = 'dev' ORDER BY started_at DESC, burst_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return BurstRecord(**dict(row)) if row else None

    def running_bursts(
        self, repository: str | None = None, *, orphaned: bool = False
    ) -> list[BurstRecord]:
        # a burst carries no repository column and no lease — the task it belongs
        # to does (C4, E1), so both scopes join it. `orphaned`: running bursts of
        # tasks no live lease covers — a live lease is a peer's proof of life,
        # from any host, and the reconciler leaves those alone.
        query = "SELECT b.* FROM bursts b LEFT JOIN tasks t ON t.task_id = b.task_id WHERE b.status = 'running'"
        params: list = []
        if repository is not None:
            query += " AND t.repository = ?"
            params.append(repository)
        if orphaned:
            query += " AND (t.claimed_until IS NULL OR t.claimed_until < ?)"
            params.append(utc_now())
        rows = self._conn.execute(query + " ORDER BY b.started_at, b.burst_id", params).fetchall()
        return [BurstRecord(**dict(row)) for row in rows]

    def bursts_started_since(self, since_iso: str, repository: str | None = None) -> int:
        """The count since an *instant*, which is not the same as since a string.

        SQLite has no timestamp type, so this is a text comparison — sound only
        because every stored stamp is `utc_now()`-spelled and that format sorts
        chronologically ('+' < '.' < digits, so an absent microsecond field sorts
        before a present one). The caller's stamp gets the same spelling via
        `utc_iso`; without it, an offset like +06:00 compares by clock reading and
        the daily rail counts the wrong bursts (gate A7). Scoped to one product's
        tasks when `repository` is given — the per-product daily quota (C4).
        """
        if repository is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM bursts WHERE started_at >= ?", (utc_iso(since_iso),)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM bursts b JOIN tasks t ON t.task_id = b.task_id "
                "WHERE b.started_at >= ? AND t.repository = ?",
                (utc_iso(since_iso), repository),
            ).fetchone()
        return int(row["n"])

    def touch_repository_burst(self, repository: str, at: str) -> None:
        """Stamp the least-recently-served cursor (C4) — the fleet marks the
        product whose turn it just took. Operational state, not an operator
        mutation: no audit row, no event, `updated_at` untouched."""
        cursor = self._conn.execute(
            "UPDATE repositories SET last_burst_at = ? WHERE repository = ?", (at, repository)
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            log.warning("touch of an unregistered product ignored", repo=repository)

    def claim_observation(self, repository: str, replica: str, lease_s: float) -> bool:
        """The observation lease (E2): the product is `replica`'s to observe for
        `lease_s` if nobody's lease on it is live. The same statement as the task
        claim, on the registry row — so a fleet of N observes a product once per
        interval, not N times. Operational state: no audit row, no event."""
        now = utc_now()
        cursor = self._conn.execute(
            "UPDATE repositories SET claimed_by = :replica, claimed_until = :until "
            "WHERE repository = :repository AND (claimed_until IS NULL OR claimed_until < :now)",
            {"replica": replica, "until": _after(now, lease_s), "repository": repository, "now": now},
        )
        self._conn.commit()
        return cursor.rowcount == 1

    # --- wakes (E5) ---

    def wake_repository(
        self, repository: str, kind: str, delivery: str | None = None, *, settle_s: float = WAKE_SETTLE_S
    ) -> bool:
        """A webhook said this product changed: take the observation lease from
        whoever holds it and reserve it — nobody's, until `settle_s` from now —
        so the next look happens once the change has settled (E6: GitHub's read
        side lags its webhook), then is anyone's; and announce it. The lease is
        a rate, not a correctness wall (E2 — observe is idempotent by CAS), so
        taking a live one costs at most one extra poll, which is the whole price
        of a forged or replayed delivery. A wake inside a pending window joins it
        rather than pushing it, so a burst of deliveries is one look, not a chase.
        False for a product that is not active — paused products are not
        observed, woken or not — and nothing is written."""
        now = utc_now()
        cursor = self._conn.execute(
            "UPDATE repositories SET claimed_until = CASE"
            "        WHEN claimed_by IS NULL AND claimed_until > :now THEN claimed_until"
            "        ELSE :settled END,"
            "    claimed_by = NULL "
            "WHERE repository = :repository AND state = :active",
            {"now": now, "settled": _after(now, settle_s), "repository": repository, "active": ProductState.ACTIVE},
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            return False
        self._conn.execute(
            "INSERT INTO events (kind, payload, created_at) VALUES (?, ?, ?)",
            (EVENT_PRODUCT_WOKEN,
             json.dumps({"repository": repository, "kind": kind, "delivery": delivery}),
             utc_now()),
        )
        self._conn.commit()
        return True

    def subscribe_wakes(self, on_wake: Callable[[str], None]) -> None:
        """Nothing to subscribe to: the single-node ledger has one process, and
        that process's own wake endpoint nudges its own loop. `on_wake` is never
        called — the Postgres ledger's LISTEN is what this stands in for."""
        log.debug("single-node ledger — wakes are in-process, nothing to listen on")

    # --- the org STOP (E3) ---
    #
    # One row for the whole ledger. Read by every tick, every spawn and every
    # heartbeat; written by the two verbs, each a compare-and-set on
    # `stop_reason` with its audit row and event in the same transaction — the
    # registry's discipline, on the org.

    def org_stop(self) -> OrgStop | None:
        row = self._conn.execute(
            "SELECT stop_reason, stopped_by, stopped_at FROM org_state"
        ).fetchone()
        if row is None or row["stop_reason"] is None:
            return None
        return OrgStop(reason=row["stop_reason"], stopped_by=row["stopped_by"], stopped_at=row["stopped_at"])

    def stop_org(self, actor: str, reason: str) -> None:
        _check_stop_reason(reason)
        cursor = self._conn.execute(
            "UPDATE org_state SET stop_reason = ?, stopped_by = ?, stopped_at = ? WHERE stop_reason IS NULL",
            (reason, actor, utc_now()),
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise RegistryError(_already_stopped(self.org_stop()))
        self._audit_and_announce(actor, "org.stop", ORG_TARGET, reason, EVENT_ORG_STOPPED,
                                 extra={"reason": reason}, subject=None)
        self._conn.commit()

    def resume_org(self, actor: str, reason: str = "") -> None:
        cursor = self._conn.execute(
            "UPDATE org_state SET stop_reason = NULL, stopped_by = NULL, stopped_at = NULL "
            "WHERE stop_reason IS NOT NULL"
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            raise RegistryError("the org is not stopped")
        self._audit_and_announce(actor, "org.resume", ORG_TARGET, reason, EVENT_ORG_RESUMED, subject=None)
        self._conn.commit()

    def record_violation(self, violation: Violation) -> None:
        """Record the violation and announce it — both rows, or neither.

        Postgres does this in one data-modifying CTE; SQLite has none, so the two
        inserts share one transaction instead. The guarantee callers get is the
        same: no violation without its event, no event without its violation.
        Nothing consumes the outbox in the single-node variant — there are no
        other systems to tell — but the row is written anyway, because a backend
        that skips it would make `events()` answer differently depending on which
        ledger you hold, and that is the one thing these two must never do.
        """
        data = asdict(violation)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        cursor = self._conn.execute(
            f"INSERT INTO violations ({cols}) VALUES ({placeholders})", data
        )
        self._conn.execute(
            "INSERT INTO events (kind, payload, created_at) VALUES (?, ?, ?)",
            (
                EVENT_VIOLATION_RECORDED,
                json.dumps(
                    {
                        "violation_id": cursor.lastrowid,
                        "task_id": violation.task_id,
                        "burst_id": violation.burst_id,
                        "tool": violation.tool,
                        "reason": violation.reason,
                    }
                ),
                utc_now(),
            ),
        )
        self._conn.commit()

    def violations(self, task_id: str | None = None) -> list[Violation]:
        names = [f.name for f in fields(Violation)]
        select = f"SELECT {', '.join(names)} FROM violations"
        if task_id is None:
            rows = self._conn.execute(f"{select} ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                f"{select} WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [Violation(**dict(row)) for row in rows]

    def events(self, after_id: int = 0) -> list[Event]:
        """The outbox from a cursor forward — the same read the Postgres backend
        answers, over a table nothing polls here yet."""
        rows = self._conn.execute(
            "SELECT id, kind, payload, created_at FROM events WHERE id > ? ORDER BY id",
            (after_id,),
        ).fetchall()
        return [
            Event(
                id=row["id"],
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @property
    def in_transaction(self) -> bool:
        """Whether a statement left a transaction open — the E1 assertion seam.
        Always False between calls, by construction: every method commits or
        rolls back before it returns."""
        return self._conn.in_transaction

    def close(self) -> None:
        self._conn.close()
