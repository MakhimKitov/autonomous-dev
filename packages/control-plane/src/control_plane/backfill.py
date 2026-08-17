"""The one-shot that moves the MVP's history into the Stage-A shape (A6, #17).

Two stores changed identity under the experiment's data, and neither change can be
made by SQL:

- **The task id became derived.** MVP rows are keyed by a bare issue number and
  carry the platform's clone path where the repository slug now goes. Turning `7`
  into `owner/repo#7` needs the slug, and the slug is nowhere in the file — it is
  an argument to this verb.
- **The archive became a tree.** MVP snapshots are one mutable blob per task, at
  `sessions/{task_id}/session.tar.gz`. Each becomes the snapshot of the burst that
  produced it: `sessions/{new_task_id}/{burst_id}/session.tar.gz`, with the
  checksum on the burst row, where the ledger is the authority for it.

What this reads is the single-node pair — a SQLite ledger and a local archive
directory — and what it writes is whatever `LEDGER_DSN` and `ARCHIVE_URL` name.
The source is opened **read-only**: this is a copy, not a move, so a failed run
leaves the origin exactly as it was and can be run again.

**Manual-era rows are dropped.** M0/M1 rows have ids like `t1` — smoke tests, not
issues. A task whose id is not a bare integer cannot be an issue, so it and its
bursts are skipped and counted. Their violations go too: the row is evidence about
a run, and a run that never was an issue is not part of the experiment's dataset.

**Events are not carried over.** The outbox exists to tell other systems what
happened *now*; replaying a year of historical violations into it would page
someone about the past. The violations themselves come across, which is where that
history actually lives.

**It refuses rather than repeats.** If the destination already holds tasks for this
repository, the verb reports that and writes nothing. A second run is a mistake
worth catching, not an idempotent no-op to be papered over — the ids are derived,
so a re-run would collide on every row anyway.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from control_plane.models import BurstRecord, TaskRecord, Violation, task_id_for
from control_plane.store import (
    LEDGER_FILENAME,
    Ledger,
    SessionArchive,
    session_key,
)

log = structlog.get_logger()

# The MVP's archive layout, which this is the last code to know about.
MVP_KEY = "sessions/{task_id}/session.tar.gz"


class BackfillError(RuntimeError):
    """The backfill cannot run: no source, or a destination that already has one."""


@dataclass(slots=True)
class Plan:
    """What a backfill would do, or did. Printed by --dry-run, logged either way."""

    repository: str
    tasks: list[tuple[str, str]] = field(default_factory=list)  # (old id, new id)
    dropped: list[str] = field(default_factory=list)  # manual-era ids
    bursts: int = 0
    violations: int = 0
    snapshots: list[tuple[str, str]] = field(default_factory=list)  # (old key, new key)
    orphaned_blobs: list[str] = field(default_factory=list)  # a pointer with no burst

    def render(self) -> str:
        lines = [
            f"backfill plan for {self.repository}",
            f"  tasks:      {len(self.tasks)} carried, {len(self.dropped)} dropped (manual-era)",
            f"  bursts:     {self.bursts}",
            f"  violations: {self.violations}",
            f"  snapshots:  {len(self.snapshots)} re-keyed",
        ]
        lines += [f"    {old} → {new}" for old, new in self.tasks]
        lines += [f"    dropped: {old}" for old in self.dropped]
        lines += [f"    {old} → {new}" for old, new in self.snapshots]
        lines += [f"    orphaned blob (no burst to attach it to): {key}" for key in self.orphaned_blobs]
        return "\n".join(lines)


def _issue_number(task_id: str) -> int | None:
    """The issue this MVP id names, or None if it never named one."""
    return int(task_id) if task_id.isdigit() else None


def _open_source(data_dir: Path) -> sqlite3.Connection:
    """The MVP ledger, read-only and by raw SQL.

    Not through `SqliteLedger`: that class speaks the *current* shape and refuses
    this file on purpose. Reading the old shape is this module's whole job, and it
    is the only place that should know what the old shape was.
    """
    path = data_dir / LEDGER_FILENAME
    if not path.is_file():
        raise BackfillError(f"no ledger to read at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def plan_backfill(data_dir: Path, repository: str, destination: Ledger) -> Plan:
    """Work out what to carry over, without touching anything."""
    source = _open_source(data_dir)
    try:
        plan = Plan(repository=repository)
        task_columns = _columns(source, "tasks")
        for row in source.execute("SELECT * FROM tasks ORDER BY created_at"):
            number = _issue_number(row["task_id"])
            if number is None:
                plan.dropped.append(row["task_id"])
                continue
            new_id = task_id_for(repository, number)
            if destination.get_task(new_id) is not None:
                raise BackfillError(
                    f"{new_id} is already in the destination — this looks like a second run. "
                    "The ids are derived, so every row would collide; nothing was written."
                )
            plan.tasks.append((row["task_id"], new_id))

            bursts = source.execute(
                "SELECT burst_id FROM bursts WHERE task_id = ? ORDER BY started_at",
                (row["task_id"],),
            ).fetchall()
            plan.bursts += len(bursts)
            plan.violations += source.execute(
                "SELECT count(*) AS n FROM violations WHERE task_id = ?", (row["task_id"],)
            ).fetchone()["n"]

            # the task's one blob becomes the last burst's snapshot
            if "transcript_key" in task_columns and row["transcript_key"]:
                leaf = _archiving_leaf(source, row["task_id"])
                old_key = MVP_KEY.format(task_id=row["task_id"])
                if leaf is None:
                    plan.orphaned_blobs.append(old_key)
                else:
                    plan.snapshots.append((old_key, session_key(new_id, leaf)))
        return plan
    finally:
        source.close()


def _archiving_leaf(source: sqlite3.Connection, task_id: str) -> str | None:
    """The burst whose run produced the task's blob: its newest dev burst.

    The MVP archived on every dev harvest and overwrote the same key, so the blob
    that survived is the last dev burst's. Reviewer and tester bursts never wrote
    it, which is why role is part of the question.
    """
    row = source.execute(
        "SELECT burst_id FROM bursts WHERE task_id = ? AND role = 'dev' "
        "ORDER BY started_at DESC, burst_id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return None if row is None else row["burst_id"]


def run_backfill(
    data_dir: Path,
    repository: str,
    destination: Ledger,
    source_archive: SessionArchive,
    destination_archive: SessionArchive,
    dry_run: bool = False,
) -> Plan:
    """Carry the experiment's history over. Returns what was (or would be) done."""
    plan = plan_backfill(data_dir, repository, destination)
    if dry_run:
        return plan

    source = _open_source(data_dir)
    try:
        renamed = dict(plan.tasks)
        snapshots = dict(plan.snapshots)
        for old_id, new_id in plan.tasks:
            row = source.execute("SELECT * FROM tasks WHERE task_id = ?", (old_id,)).fetchone()
            number = _issue_number(old_id)
            assert number is not None  # plan_backfill kept only these
            destination.upsert_task(
                TaskRecord(
                    task_id=new_id,
                    repository=repository,
                    issue_number=number,
                    status=row["status"],
                    session_id=row["session_id"],
                    pr_number=row["pr_number"],
                    attempts=row["attempts"],
                    review_rounds=row["review_rounds"],
                    next_burst_kind=row["next_burst_kind"],
                    last_review_id=row["last_review_id"],
                    last_burst_at=row["last_burst_at"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        burst_columns = _columns(source, "bursts")
        for old_id, new_id in plan.tasks:
            for row in source.execute(
                "SELECT * FROM bursts WHERE task_id = ? ORDER BY started_at", (old_id,)
            ):
                burst = _burst_from(row, new_id, burst_columns)
                old_key = MVP_KEY.format(task_id=old_id)
                new_key = snapshots.get(old_key)
                if new_key is not None and new_key.endswith(f"/{burst.burst_id}/session.tar.gz"):
                    burst.session_key = new_key
                    burst.session_checksum = _move_snapshot(
                        source_archive, destination_archive, old_key, new_key
                    )
                _insert_finished(destination, burst)

            for row in source.execute(
                "SELECT * FROM violations WHERE task_id = ? ORDER BY id", (old_id,)
            ):
                destination.record_violation(
                    Violation(
                        task_id=new_id,
                        burst_id=row["burst_id"],
                        tool=row["tool"],
                        reason=row["reason"],
                        detail=row["detail"],
                        at=row["at"],
                    )
                )

        log.info(
            "backfill complete",
            repository=repository,
            tasks=len(renamed),
            dropped=len(plan.dropped),
            bursts=plan.bursts,
            violations=plan.violations,
            snapshots=len(plan.snapshots),
        )
        return plan
    finally:
        source.close()


def _burst_from(row: sqlite3.Row, task_id: str, columns: set[str]) -> BurstRecord:
    """An MVP burst row as a current `BurstRecord`, under its task's new id."""

    def value(name: str):
        return row[name] if name in columns else None

    return BurstRecord(
        burst_id=row["burst_id"],
        task_id=task_id,
        kind=row["kind"],
        role=row["role"],
        status=row["status"],
        trace_id=row["trace_id"] or "",
        resumed_from_burst_id=value("resumed_from_burst_id"),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        exit_code=row["exit_code"],
        model=value("model"),
        cost_usd=row["cost_usd"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        num_turns=row["num_turns"],
        session_id=row["session_id"],
    )


def _insert_finished(destination: Ledger, burst: BurstRecord) -> None:
    """Insert a historical burst, results and all.

    `record_burst` writes what is known at spawn and `finish_burst` writes the
    outcome — a split that matters when a burst is running and means nothing for one
    that ended years ago. The finalize CAS needs the row to be `running` to match,
    so the insert claims that briefly and the finalize immediately corrects it,
    which also keeps the `running ⟺ finished_at` wall satisfied at every step.
    """
    status, finished_at = burst.status, burst.finished_at
    session_key_value, checksum = burst.session_key, burst.session_checksum
    burst.status, burst.finished_at = "running", None
    burst.session_key = burst.session_checksum = None
    destination.record_burst(burst)

    burst.status, burst.finished_at = status, finished_at
    burst.session_key, burst.session_checksum = session_key_value, checksum
    if status != "running":
        destination.finish_burst(burst)


def _move_snapshot(
    source: SessionArchive, destination: SessionArchive, old_key: str, new_key: str
) -> str:
    """Copy one snapshot to its new key, returning the sha256 the ledger will hold.

    Read through the source's `get`, which verifies the bytes against the checksum
    the MVP recorded — so a blob that rotted on disk stops the backfill instead of
    being carried over as if it were fine. Written through the destination's `put`,
    which re-tars the unpacked content and computes its own sha256. The two numbers
    are *not* the same and shouldn't be: a tarball is not a canonical encoding of a
    directory, so the new object gets the checksum of the bytes that actually exist
    at the new key. That is what the ledger records, and what a later `get` checks.
    """
    import tempfile

    recorded = source.recorded_checksum(old_key)
    if recorded is None:
        raise BackfillError(f"{old_key} has no recorded checksum — refusing to carry it blind")
    with tempfile.TemporaryDirectory() as staging:
        unpacked = Path(staging) / "session"
        source.get(old_key, unpacked, recorded)
        checksum = destination.put(new_key, unpacked)
    log.info("snapshot re-keyed", old=old_key, new=new_key)
    return checksum
