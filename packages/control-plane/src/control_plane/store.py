"""Platform-owned persistence: the session archive and the ledger.

The agent holds no storage credentials — all I/O here happens in the control
plane, out-of-band of the burst (ARCHITECTURE.md §6 "State & storage"). Two
stores, two protocols:

- ``SessionArchive`` — the raw-transcript blob store. Phase-0 backend is a local
  directory of tarballs; the SeaweedFS (S3) backend implements the same protocol
  later without touching callers.
- ``Ledger`` — the queryable index (tasks, burst ledger, violations). Phase-0
  backend is SQLite (stdlib); Postgres later, same protocol.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tarfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Protocol

from control_plane.models import BurstRecord, TaskRecord, Violation, utc_now


class SessionArchive(Protocol):
    def put(self, task_id: str, session_dir: Path) -> tuple[str, str]:
        """Archive a session volume. Returns (key, sha256)."""
        ...

    def get(self, task_id: str, dest_dir: Path) -> bool:
        """Materialize the task's session into dest_dir. False if none stored."""
        ...


# Credentials must never reach the archive. With CLAUDE_CONFIG_DIR set, the CLI
# stores .credentials.json under it — i.e. on the session volume we harvest.
# Env-token auth shouldn't write it, but the exclusion is unconditional.
EXCLUDE_FROM_ARCHIVE = {".credentials.json"}


class LocalSessionArchive:
    """Phase-0 archive: one tarball per task under a local root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _blob(self, task_id: str) -> Path:
        return self.root / task_id / "session.tar.gz"

    def put(self, task_id: str, session_dir: Path) -> tuple[str, str]:
        blob = self._blob(task_id)
        blob.parent.mkdir(parents=True, exist_ok=True)

        def _skip_credentials(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            return None if Path(info.name).name in EXCLUDE_FROM_ARCHIVE else info

        with tarfile.open(blob, "w:gz") as tar:
            tar.add(session_dir, arcname=".", filter=_skip_credentials)
        checksum = hashlib.sha256(blob.read_bytes()).hexdigest()
        (blob.parent / "sha256").write_text(checksum, encoding="utf-8")
        return str(blob.relative_to(self.root)), checksum

    def get(self, task_id: str, dest_dir: Path) -> bool:
        blob = self._blob(task_id)
        if not blob.is_file():
            return False
        recorded = (blob.parent / "sha256").read_text(encoding="utf-8").strip()
        actual = hashlib.sha256(blob.read_bytes()).hexdigest()
        if recorded != actual:
            raise RuntimeError(f"transcript checksum mismatch for task {task_id}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(blob, "r:gz") as tar:
            tar.extractall(dest_dir, filter="data")
        return True


class Ledger(Protocol):
    def upsert_task(self, task: TaskRecord) -> None: ...
    def get_task(self, task_id: str) -> TaskRecord | None: ...
    def record_burst(self, burst: BurstRecord) -> None: ...
    def finish_burst(self, burst: BurstRecord) -> None: ...
    def record_violation(self, violation: Violation) -> None: ...
    def violations(self, task_id: str | None = None) -> list[Violation]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    status TEXT NOT NULL,
    session_id TEXT,
    transcript_key TEXT,
    transcript_checksum TEXT,
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
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    num_turns INTEGER,
    session_id TEXT
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
"""


class SqliteLedger:
    """Phase-0 ledger: stdlib SQLite. Same protocol Postgres implements later."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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

    def record_burst(self, burst: BurstRecord) -> None:
        data = asdict(burst)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        self._conn.execute(f"INSERT INTO bursts ({cols}) VALUES ({placeholders})", data)
        self._conn.commit()

    def finish_burst(self, burst: BurstRecord) -> None:
        self._conn.execute(
            "UPDATE bursts SET status=:status, finished_at=:finished_at, exit_code=:exit_code, "
            "cost_usd=:cost_usd, input_tokens=:input_tokens, output_tokens=:output_tokens, "
            "num_turns=:num_turns, session_id=:session_id "
            "WHERE burst_id=:burst_id",
            asdict(burst),
        )
        self._conn.commit()

    def record_violation(self, violation: Violation) -> None:
        data = asdict(violation)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{k}" for k in data)
        self._conn.execute(f"INSERT INTO violations ({cols}) VALUES ({placeholders})", data)
        self._conn.commit()

    def violations(self, task_id: str | None = None) -> list[Violation]:
        names = [f.name for f in fields(Violation)]
        select = f"SELECT {', '.join(names)} FROM violations"
        if task_id is None:
            rows = self._conn.execute(select).fetchall()
        else:
            rows = self._conn.execute(f"{select} WHERE task_id = ?", (task_id,)).fetchall()
        return [Violation(**dict(row)) for row in rows]

    def close(self) -> None:
        self._conn.close()
