"""The one-shot, against a realistic MVP ledger (A6, #17).

The issue's *done when* is a comparison — "migrated experiment data answers
identical protocol queries on both stores" — so the central test is one: build the
single-node pair the MVP actually left behind, carry it over, and check the
destination's protocol answers field by field against the source's raw rows. The
id rewrite is the only thing allowed to differ.

The source here is built with raw SQL in the MVP's shape on purpose. `SqliteLedger`
refuses that shape now (A6 retires it as an origin), and the backfill is the only
code left that knows what it looked like — so the fixture has to write it directly
rather than through a class that would reject it.

Postgres is skipped unless `LEDGER_DSN` is set; the archive half runs on the local
backend either way, and on a bucket too when `ARCHIVE_URL` is set.
"""

import sqlite3
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from control_plane.backfill import MVP_KEY, BackfillError, plan_backfill, run_backfill
from control_plane.migrate import migrate
from control_plane.models import task_id_for
from control_plane.store import (
    ARCHIVE_URL_ENV,
    LEDGER_DSN_ENV,
    LocalSessionArchive,
    session_key,
    tar_without_credentials,
)
from psycopg.conninfo import make_conninfo

import os

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
# read at import, like the contract suites: conftest.py unsets both variables for
# every test, so a fixture that consulted the environment would get the local
# backend no matter what the job configured
ARCHIVE = os.environ.get(ARCHIVE_URL_ENV, "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset — no destination ledger")

REPOSITORY = "owner/product"

# The MVP's tables, verbatim enough to be the thing we migrated from: `repo` holds
# a clone path, `task_id` is a bare issue number, and the transcript pointer is on
# the task rather than the burst.
MVP_SCHEMA = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY, repo TEXT NOT NULL, status TEXT NOT NULL, session_id TEXT,
    transcript_key TEXT, transcript_checksum TEXT, pr_number INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0, review_rounds INTEGER NOT NULL DEFAULT 0,
    next_burst_kind TEXT, last_review_id INTEGER, last_burst_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE bursts (
    burst_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL,
    status TEXT NOT NULL, trace_id TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
    finished_at TEXT, exit_code INTEGER, model TEXT, cost_usd REAL, input_tokens INTEGER,
    output_tokens INTEGER, num_turns INTEGER, session_id TEXT
);
CREATE TABLE violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, burst_id TEXT NOT NULL,
    tool TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', at TEXT NOT NULL
);
"""


@pytest.fixture
def destination():
    """An empty migrated ledger of its own, dropped afterwards."""
    from control_plane.postgres import PostgresLedger

    name = f"backfill_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    ledger = PostgresLedger(dsn)
    try:
        yield ledger
    finally:
        ledger.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(params=["local"] + (["s3"] if ARCHIVE else []))
def destination_archive(request, tmp_path):
    """Where snapshots land: a directory, and a bucket when one is configured."""
    if request.param == "local":
        return LocalSessionArchive(tmp_path / "destination")
    from control_plane.objectstore import ObjectSessionArchive

    archive = ObjectSessionArchive.from_url(ARCHIVE)
    archive.prefix = f"{archive.prefix}/backfill-{uuid4().hex[:8]}".strip("/")
    return archive


@pytest.fixture
def mvp(tmp_path):
    """The single-node pair the MVP left: a ledger, and one blob per task.

    Task 7 ran twice as the dev with a reviewer burst between, and has a
    transcript. Task 9 ran once and recorded a violation. `t1` is a boot smoke from
    M0 — an id that was never an issue.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "ledger.sqlite3")
    conn.executescript(MVP_SCHEMA)
    archive = LocalSessionArchive(data_dir)

    for task_id, status, pr, transcript in [
        ("7", "done", 12, True),
        ("9", "awaiting_review", 14, True),
        ("t1", "open", None, False),
    ]:
        conn.execute(
            "INSERT INTO tasks (task_id, repo, status, session_id, transcript_key, "
            "transcript_checksum, pr_number, attempts, review_rounds, next_burst_kind, "
            "last_review_id, last_burst_at, created_at, updated_at) "
            "VALUES (?, '/Users/x/clones/product', ?, ?, ?, ?, ?, 2, 1, 'review', 900, ?, ?, ?)",
            (
                task_id,
                status,
                f"sess-{task_id}",
                MVP_KEY.format(task_id=task_id) if transcript else None,
                "a" * 64 if transcript else None,
                pr,
                f"2026-07-0{task_id[-1]}T09:00:00+00:00",
                f"2026-07-0{task_id[-1]}T08:00:00+00:00",
                f"2026-07-0{task_id[-1]}T09:00:00+00:00",
            ),
        )

    for burst_id, task_id, role, kind, started, cost in [
        ("b1", "7", "dev", "work", "2026-07-07T08:10:00+00:00", 0.42),
        ("b2", "7", "reviewer", "assess", "2026-07-07T08:20:00+00:00", 0.11),
        ("b3", "7", "dev", "work", "2026-07-07T08:30:00+00:00", 0.31),
        ("b4", "9", "dev", "work", "2026-07-09T08:10:00+00:00", 0.19),
        ("b5", "t1", "dev", "work", "2026-07-01T08:00:00+00:00", None),
    ]:
        conn.execute(
            "INSERT INTO bursts (burst_id, task_id, kind, role, status, trace_id, started_at, "
            "finished_at, exit_code, model, cost_usd, input_tokens, output_tokens, num_turns, "
            "session_id) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, 0, 'claude-sonnet-5', ?, "
            "1200, 340, 7, ?)",
            (burst_id, task_id, kind, role, "0" * 32, started, started, cost, f"sess-{task_id}"),
        )

    conn.execute(
        "INSERT INTO violations (task_id, burst_id, tool, reason, detail, at) "
        "VALUES ('9', 'b4', 'Read', 'WALL: private/', '{}', '2026-07-09T08:11:00+00:00')"
    )
    conn.commit()
    conn.close()

    # the blobs those transcript pointers name
    for task_id in ("7", "9"):
        volume = tmp_path / f"volume-{task_id}"
        (volume / "projects").mkdir(parents=True)
        (volume / "projects" / "sess.jsonl").write_text(f'{{"task":"{task_id}"}}\n', encoding="utf-8")
        blob = data_dir / MVP_KEY.format(task_id=task_id)
        blob.parent.mkdir(parents=True, exist_ok=True)
        checksum = tar_without_credentials(volume, blob)
        blob.with_suffix(".gz.sha256").write_text(checksum, encoding="utf-8")

    return data_dir, archive


def _source(data_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(data_dir / "ledger.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


# --- the comparison the issue asks for ---


def test_the_migrated_data_answers_the_same_questions(mvp, destination, destination_archive):
    """Field by field, source against destination. The id is the only thing that
    changes, and `issue_number` is the only thing that appears."""
    data_dir, source_archive = mvp
    run_backfill(data_dir, REPOSITORY, destination, source_archive, destination_archive)

    source = _source(data_dir)
    try:
        for row in source.execute("SELECT * FROM tasks WHERE task_id IN ('7', '9')"):
            task = destination.get_task(task_id_for(REPOSITORY, int(row["task_id"])))
            assert task is not None
            assert (task.repository, task.issue_number) == (REPOSITORY, int(row["task_id"]))
            for field in (
                "status",
                "session_id",
                "pr_number",
                "attempts",
                "review_rounds",
                "next_burst_kind",
                "last_review_id",
                "last_burst_at",
                "created_at",
            ):
                assert getattr(task, field) == row[field], field

        for row in source.execute("SELECT * FROM bursts WHERE task_id IN ('7', '9')"):
            burst = destination.get_burst(row["burst_id"])
            assert burst is not None
            assert burst.task_id == task_id_for(REPOSITORY, int(row["task_id"]))
            for field in (
                "kind",
                "role",
                "status",
                "trace_id",
                "started_at",
                "finished_at",
                "exit_code",
                "model",
                "cost_usd",
                "input_tokens",
                "output_tokens",
                "num_turns",
                "session_id",
            ):
                assert getattr(burst, field) == row[field], field

        moved = destination.violations(task_id_for(REPOSITORY, 9))
        original = source.execute("SELECT * FROM violations").fetchall()
        assert len(moved) == len(original) == 1
        for field in ("burst_id", "tool", "reason", "detail", "at"):
            assert getattr(moved[0], field) == original[0][field], field
    finally:
        source.close()


def test_the_snapshot_becomes_the_last_dev_bursts(mvp, destination, destination_archive, tmp_path):
    """One mutable blob per task becomes one immutable snapshot per burst — and
    which burst is not a guess: the MVP overwrote the key on every dev harvest, so
    the blob that survived is the newest dev burst's. b2 is a reviewer and b3 is
    newer than b1."""
    data_dir, source_archive = mvp
    run_backfill(data_dir, REPOSITORY, destination, source_archive, destination_archive)

    task_id = task_id_for(REPOSITORY, 7)
    leaf = destination.latest_archived_burst(task_id)
    assert leaf.burst_id == "b3"
    assert leaf.session_key == session_key(task_id, "b3")
    assert destination.get_burst("b1").session_key is None
    assert destination.get_burst("b2").session_key is None  # the reviewer, never a node

    # and the bytes are there, under the checksum the ledger now records
    restored = tmp_path / "restored"
    destination_archive.get(leaf.session_key, restored, leaf.session_checksum)
    assert (restored / "projects" / "sess.jsonl").read_text() == '{"task":"7"}\n'


def test_manual_era_rows_are_dropped(mvp, destination, destination_archive):
    """`t1` was a boot smoke, not an issue. It cannot be given a derived id, so it
    and its burst stay behind — counted, not silently skipped."""
    data_dir, source_archive = mvp
    plan = run_backfill(data_dir, REPOSITORY, destination, source_archive, destination_archive)

    assert plan.dropped == ["t1"]
    assert destination.get_burst("b5") is None
    assert [task.task_id for task in destination.tasks_by_status("open")] == []
    assert plan.bursts == 4  # b1..b4; b5 belonged to t1


def test_the_source_is_left_exactly_as_it_was(mvp, destination, destination_archive):
    """A copy, not a move: a failed or half-finished run must leave the origin
    runnable, and the origin is the only place this history exists."""
    data_dir, source_archive = mvp
    before = (data_dir / "ledger.sqlite3").read_bytes()
    blob = data_dir / MVP_KEY.format(task_id="7")
    blob_before = blob.read_bytes()

    run_backfill(data_dir, REPOSITORY, destination, source_archive, destination_archive)

    assert (data_dir / "ledger.sqlite3").read_bytes() == before
    assert blob.read_bytes() == blob_before


def test_a_second_run_refuses(mvp, destination, destination_archive):
    """The ids are derived, so a re-run would collide on every row. Better to say
    so than to half-apply or to quietly do nothing."""
    data_dir, source_archive = mvp
    run_backfill(data_dir, REPOSITORY, destination, source_archive, destination_archive)

    with pytest.raises(BackfillError, match="second run"):
        run_backfill(data_dir, REPOSITORY, destination, source_archive, destination_archive)


def test_dry_run_writes_nothing(mvp, destination, destination_archive):
    data_dir, source_archive = mvp
    plan = run_backfill(
        data_dir, REPOSITORY, destination, source_archive, destination_archive, dry_run=True
    )

    assert [old for old, _ in plan.tasks] == ["7", "9"]
    assert plan.dropped == ["t1"]
    assert destination.get_task(task_id_for(REPOSITORY, 7)) is None
    assert destination_archive.recorded_checksum(session_key(task_id_for(REPOSITORY, 7), "b3")) is None
    assert "7 → owner/product#7" in plan.render()


def test_a_blob_with_no_dev_burst_is_reported_not_dropped_silently(mvp, destination, destination_archive):
    """A transcript pointer whose bursts have all vanished has nowhere to attach.
    That is worth naming in the plan — it is the one thing the backfill knows it
    could not carry."""
    data_dir, source_archive = mvp
    conn = sqlite3.connect(data_dir / "ledger.sqlite3")
    conn.execute("DELETE FROM bursts WHERE task_id = '9'")
    conn.commit()
    conn.close()

    plan = plan_backfill(data_dir, REPOSITORY, destination)
    assert plan.orphaned_blobs == [MVP_KEY.format(task_id="9")]
    assert "orphaned blob" in plan.render()


def test_no_source_says_so(tmp_path, destination):
    with pytest.raises(BackfillError, match="no ledger to read"):
        plan_backfill(tmp_path, REPOSITORY, destination)
