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
from dataclasses import asdict, fields
from pathlib import Path
from typing import Protocol

import structlog

from control_plane.models import (
    BURST_FINALIZE_COLUMNS,
    EVENT_PRODUCT_PAUSED,
    EVENT_PRODUCT_REGISTERED,
    EVENT_PRODUCT_RESUMED,
    EVENT_PRODUCT_UPDATED,
    EVENT_VIOLATION_RECORDED,
    AuditRecord,
    BurstRecord,
    Event,
    ProductState,
    RepositoryRecord,
    TaskRecord,
    Violation,
    utc_iso,
    utc_now,
)

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


def _check_settable(changes: dict) -> None:
    if not changes:
        raise RegistryError("nothing to set — name at least one column to change")
    unknown = sorted(set(changes) - set(PRODUCT_SET_COLUMNS))
    if unknown:
        raise RegistryError(
            f"not settable: {', '.join(unknown)} — `product set` touches only "
            f"{', '.join(PRODUCT_SET_COLUMNS)}"
        )


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
    def audit_trail(self, target: str | None = None) -> list[AuditRecord]: ...
    def upsert_task(self, task: TaskRecord) -> None: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def tasks_by_status(
        self, *statuses: str, repository: str | None = None
    ) -> list[TaskRecord]: ...
    def record_burst(self, burst: BurstRecord) -> None: ...
    def finish_burst(self, burst: BurstRecord) -> None: ...
    def get_burst(self, burst_id: str) -> BurstRecord | None: ...
    def latest_archived_burst(self, task_id: str) -> BurstRecord | None: ...
    def running_bursts(self, repository: str | None = None) -> list[BurstRecord]: ...
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
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
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
        self._conn = sqlite3.connect(path)
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
                            kind: str, extra: dict | None = None) -> None:
        """The two rows every registry mutation trails. Caller commits."""
        cursor = self._conn.execute(
            "INSERT INTO audit (actor, action, target, reason, at) VALUES (?, ?, ?, ?, ?)",
            (actor, action, target, reason, utc_now()),
        )
        payload = {"audit_id": cursor.lastrowid, "repository": target, "actor": actor, **(extra or {})}
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

    # --- tasks ---

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

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return TaskRecord(**dict(row)) if row else None

    def tasks_by_status(
        self, *statuses: str, repository: str | None = None
    ) -> list[TaskRecord]:
        # `task_id` breaks the tie: the scheduler takes the first row as "oldest
        # first", and two tasks created in the same microsecond would otherwise be
        # ordered by whatever the storage happened to return (gate A7). The
        # repository scope is C4: a product's scheduler sees only its own tasks.
        marks = ", ".join("?" for _ in statuses)
        query = f"SELECT * FROM tasks WHERE status IN ({marks})"
        params: list = list(statuses)
        if repository is not None:
            query += " AND repository = ?"
            params.append(repository)
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

    def running_bursts(self, repository: str | None = None) -> list[BurstRecord]:
        # a burst carries no repository column — the task it belongs to does (C4)
        if repository is None:
            rows = self._conn.execute(
                "SELECT * FROM bursts WHERE status = 'running' ORDER BY started_at, burst_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT b.* FROM bursts b JOIN tasks t ON t.task_id = b.task_id "
                "WHERE b.status = 'running' AND t.repository = ? "
                "ORDER BY b.started_at, b.burst_id",
                (repository,),
            ).fetchall()
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

    def close(self) -> None:
        self._conn.close()
