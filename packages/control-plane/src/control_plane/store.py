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
    EVENT_VIOLATION_RECORDED,
    BurstRecord,
    Event,
    TaskRecord,
    Violation,
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
    def upsert_task(self, task: TaskRecord) -> None: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def tasks_by_status(self, *statuses: str) -> list[TaskRecord]: ...
    def record_burst(self, burst: BurstRecord) -> None: ...
    def finish_burst(self, burst: BurstRecord) -> None: ...
    def get_burst(self, burst_id: str) -> BurstRecord | None: ...
    def latest_archived_burst(self, task_id: str) -> BurstRecord | None: ...
    def running_bursts(self) -> list[BurstRecord]: ...
    def bursts_started_since(self, since_iso: str) -> int: ...
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
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    transcript_key TEXT,
    transcript_checksum TEXT,
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


class SqliteLedger:
    """Phase-0 ledger: stdlib SQLite. Same protocol Postgres implements later."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Phase-0 migration: additive columns only (CREATE IF NOT EXISTS won't
        # touch a ledger created before the column existed).
        for table, column, decl in (
            ("bursts", "model", "TEXT"),
            # the session tree (A5): a ledger written before it gains the columns
            ("bursts", "resumed_from_burst_id", "TEXT"),
            ("bursts", "session_key", "TEXT"),
            ("bursts", "session_checksum", "TEXT"),
            ("tasks", "pr_number", "INTEGER"),
            ("tasks", "attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("tasks", "review_rounds", "INTEGER NOT NULL DEFAULT 0"),
            ("tasks", "next_burst_kind", "TEXT"),
            ("tasks", "last_review_id", "INTEGER"),
            ("tasks", "last_burst_at", "TEXT"),
        ):
            self._ensure_column(table, column, decl)
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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

    def tasks_by_status(self, *statuses: str) -> list[TaskRecord]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at", statuses
        ).fetchall()
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
        """The leaf of this task's session tree: the newest burst that archived a
        snapshot. What the next burst's volume is materialized from, and what its
        `resumed_from_burst_id` points at."""
        row = self._conn.execute(
            "SELECT * FROM bursts WHERE task_id = ? AND session_key IS NOT NULL "
            "ORDER BY started_at DESC, burst_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return BurstRecord(**dict(row)) if row else None

    def running_bursts(self) -> list[BurstRecord]:
        rows = self._conn.execute("SELECT * FROM bursts WHERE status = 'running'").fetchall()
        return [BurstRecord(**dict(row)) for row in rows]

    def bursts_started_since(self, since_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM bursts WHERE started_at >= ?", (since_iso,)
        ).fetchone()
        return int(row["n"])

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
