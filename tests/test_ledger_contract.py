"""The `Ledger` contract, run against every backend there is.

One suite, parameterized over `SqliteLedger` and `PostgresLedger`. That is the
whole point of A3: the two are one design with and without contention, so a test
that passes on one and fails on the other is a bug by definition — and gate A7
(#18) is exactly this claim made about the full MVP scenario instead of about
methods.

Postgres is skipped unless `LEDGER_DSN` is set. When it is, the suite gets one
throwaway migrated database for the session and truncates between tests, so it
never touches the rig's own ledger.

Note on the vocabulary: timestamps are UTC ISO-8601 strings, because `utc_now()`
is the only thing that produces them. A non-UTC offset string is outside the
contract — Postgres would normalize it to UTC on the way back out and SQLite
would return it verbatim, which is a divergence no caller can currently provoke.
"""

import os
from dataclasses import asdict
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.models import (
    BurstKind,
    BurstRecord,
    BurstStatus,
    Role,
    TaskRecord,
    TaskStatus,
    Violation,
)
from control_plane.store import LEDGER_DSN_ENV, SqliteLedger
from psycopg.conninfo import make_conninfo

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
BACKENDS = ["sqlite"] + (["postgres"] if DSN else [])

needs_postgres = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset")


@pytest.fixture(scope="session")
def pg_dsn():
    """One migrated throwaway database, for the whole session."""
    name = f"ledger_contract_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(params=BACKENDS)
def ledger(request, tmp_path):
    """A empty ledger of each backend in turn."""
    if request.param == "sqlite":
        backend = SqliteLedger(tmp_path / "ledger.sqlite3")
    else:
        from control_plane.postgres import PostgresLedger

        dsn = request.getfixturevalue("pg_dsn")  # lazily: sqlite runs need no database
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("TRUNCATE tasks, bursts, violations")
        backend = PostgresLedger(dsn)
    yield backend
    backend.close()


def _task(task_id="42", **kwargs):
    return TaskRecord(task_id=task_id, repo="/r", **kwargs)


def _burst(burst_id="b1", task_id="42", **kwargs):
    return BurstRecord(burst_id=burst_id, task_id=task_id, **kwargs)


# --- tasks ---


def test_unknown_task_is_none(ledger):
    assert ledger.get_task("nope") is None


def test_task_roundtrip_preserves_every_field(ledger):
    task = _task(
        status=TaskStatus.AWAITING_REVIEW,
        session_id="sess-1",
        transcript_key="42/session.tar.gz",
        transcript_checksum="a" * 64,
        pr_number=7,
        attempts=2,
        review_rounds=1,
        next_burst_kind=BurstKind.REVIEW,
        last_review_id=99887766,
        last_burst_at="2026-08-14T09:15:30.123456+00:00",
    )
    ledger.upsert_task(task)

    loaded = ledger.get_task("42")
    assert loaded is not None
    # updated_at is stamped by the write, so compare everything else verbatim
    assert {k: v for k, v in asdict(loaded).items() if k != "updated_at"} == {
        k: v for k, v in asdict(task).items() if k != "updated_at"
    }


def test_upsert_keeps_created_at_and_advances_updated_at(ledger):
    task = _task(created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
    ledger.upsert_task(task)
    first = ledger.get_task("42")

    task.status = TaskStatus.WORKING
    ledger.upsert_task(task)
    second = ledger.get_task("42")

    assert second.created_at == first.created_at == "2026-01-01T00:00:00+00:00"
    assert second.updated_at > first.updated_at
    assert second.status == TaskStatus.WORKING


def test_tasks_by_status_filters_and_orders_by_created_at(ledger):
    ledger.upsert_task(_task("1", status=TaskStatus.CLAIMED, created_at="2026-01-03T00:00:00+00:00"))
    ledger.upsert_task(_task("2", status=TaskStatus.WORKING, created_at="2026-01-01T00:00:00+00:00"))
    ledger.upsert_task(_task("3", status=TaskStatus.DONE, created_at="2026-01-02T00:00:00+00:00"))

    picked = ledger.tasks_by_status(TaskStatus.CLAIMED, TaskStatus.WORKING)
    assert [t.task_id for t in picked] == ["2", "1"]  # oldest first
    assert ledger.tasks_by_status(TaskStatus.BLOCKED) == []


# --- bursts ---


def test_burst_roundtrip_and_finalize(ledger):
    ledger.upsert_task(_task())
    burst = _burst(trace_id="0" * 32, model="claude-sonnet-5", started_at="2026-08-14T09:00:00+00:00")
    ledger.record_burst(burst)

    assert ledger.get_burst("b1").status == BurstStatus.RUNNING

    burst.status = BurstStatus.SUCCEEDED
    burst.finished_at = "2026-08-14T09:04:30.500000+00:00"
    burst.exit_code = 0
    burst.cost_usd = 0.5
    burst.input_tokens = 1200
    burst.output_tokens = 340
    burst.num_turns = 7
    burst.session_id = "sess-9"
    burst.model = "claude-sonnet-5-20260203"  # harvest overwrote requested with resolved
    ledger.finish_burst(burst)

    loaded = ledger.get_burst("b1")
    assert asdict(loaded) == asdict(burst)
    assert ledger.get_burst("nope") is None


def test_finalize_happens_once_and_the_first_result_stands(ledger):
    """The CAS: a second finalize must not overwrite a run that already ended."""
    ledger.upsert_task(_task())
    burst = _burst()
    ledger.record_burst(burst)

    burst.status = BurstStatus.SUCCEEDED
    burst.finished_at = "2026-08-14T09:04:00+00:00"
    burst.exit_code = 0
    ledger.finish_burst(burst)

    loser = ledger.get_burst("b1")
    loser.status = BurstStatus.KILLED  # a reconciler racing the burst's own exit
    loser.exit_code = 137
    ledger.finish_burst(loser)

    still = ledger.get_burst("b1")
    assert still.status == BurstStatus.SUCCEEDED
    assert still.exit_code == 0


def test_running_bursts_sees_only_the_unfinished(ledger):
    ledger.upsert_task(_task())
    ledger.record_burst(_burst("b1"))
    ledger.record_burst(_burst("b2"))
    assert {b.burst_id for b in ledger.running_bursts()} == {"b1", "b2"}

    done = ledger.get_burst("b1")
    done.status = BurstStatus.FAILED
    done.finished_at = "2026-08-14T09:05:00+00:00"
    done.exit_code = 1
    ledger.finish_burst(done)

    assert [b.burst_id for b in ledger.running_bursts()] == ["b2"]


def test_bursts_started_since_counts_the_window(ledger):
    ledger.upsert_task(_task())
    ledger.record_burst(_burst("old", started_at="2026-08-13T09:00:00+00:00"))
    ledger.record_burst(_burst("new", started_at="2026-08-14T09:00:00+00:00"))

    assert ledger.bursts_started_since("2026-08-14T00:00:00+00:00") == 1
    assert ledger.bursts_started_since("2026-08-01T00:00:00+00:00") == 2
    assert ledger.bursts_started_since("2026-09-01T00:00:00+00:00") == 0


# --- violations ---


def test_violations_by_task_and_in_full(ledger):
    ledger.record_violation(Violation(task_id="42", burst_id="b1", tool="Read", reason="WALL"))
    ledger.record_violation(
        Violation(task_id="42", burst_id="b1", tool="Bash", reason="WALL", detail="rm -rf /")
    )
    ledger.record_violation(Violation(task_id="99", burst_id="b2", tool="Read", reason="WALL"))

    for_task = ledger.violations("42")
    assert [v.tool for v in for_task] == ["Read", "Bash"]  # insertion order
    assert for_task[1].detail == "rm -rf /"
    assert len(ledger.violations()) == 3
    assert ledger.violations("nobody") == []


# --- the adapter edge ---


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-08-14T09:15:30.123456+00:00",  # microseconds preserved
        "2026-08-14T09:15:30+00:00",  # and no fraction invented when there is none
    ],
)
def test_timestamps_come_back_as_the_identical_iso_string(ledger, stamp):
    """timestamptz in the database, strings at the edge — byte-identical ones."""
    ledger.upsert_task(_task(created_at=stamp))
    loaded = ledger.get_task("42")
    assert loaded.created_at == stamp
    assert isinstance(loaded.created_at, str)


def test_money_comes_back_as_float_not_decimal(ledger):
    """`cost_usd` is numeric in the schema and float in the dataclass."""
    ledger.upsert_task(_task())
    burst = _burst()
    ledger.record_burst(burst)
    burst.status = BurstStatus.SUCCEEDED
    burst.finished_at = "2026-08-14T09:05:00+00:00"
    burst.cost_usd = 1.25
    ledger.finish_burst(burst)

    cost = ledger.get_burst("b1").cost_usd
    assert type(cost) is float
    assert cost == 1.25


def test_absent_optionals_stay_none(ledger):
    ledger.upsert_task(_task())
    task = ledger.get_task("42")
    assert (task.pr_number, task.session_id, task.last_burst_at, task.last_review_id) == (
        None,
        None,
        None,
        None,
    )

    ledger.record_burst(_burst())
    burst = ledger.get_burst("b1")
    assert (burst.finished_at, burst.exit_code, burst.cost_usd, burst.num_turns) == (
        None,
        None,
        None,
        None,
    )


def test_roles_and_kinds_round_trip_as_their_string_values(ledger):
    ledger.upsert_task(_task())
    ledger.record_burst(_burst(kind=BurstKind.TEST, role=Role.TESTER))
    burst = ledger.get_burst("b1")
    assert (burst.kind, burst.role) == ("test", "tester")


# --- Postgres-only behavior ---


@needs_postgres
def test_a_dropped_connection_costs_a_reconnect_not_the_loop(pg_dsn):
    """A scheduler outlives database restarts; the next statement should just work."""
    from control_plane.postgres import PostgresLedger

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE tasks, bursts, violations")
    ledger = PostgresLedger(pg_dsn)
    ledger.upsert_task(_task())

    ledger._conn.close()  # as if Postgres restarted under us

    ledger.upsert_task(_task(status=TaskStatus.WORKING))
    assert ledger.get_task("42").status == TaskStatus.WORKING
    ledger.close()


def test_the_dsn_password_never_reaches_the_log():
    """`open_ledger` logs which backend it picked; the DSN's password isn't part
    of that."""
    from control_plane.store import _redact

    assert _redact("postgresql://platform_admin:platform@localhost:5432/platform") == (
        "postgresql://platform_admin:***@localhost:5432/platform"
    )
    # psycopg takes key=value conninfo too, and that form has a password as well
    assert _redact("host=localhost password=hunter2 dbname=platform") == (
        "host=localhost password=*** dbname=platform"
    )
    # nothing to redact when there are no credentials in it
    assert _redact("postgresql://localhost:5432/platform") == "postgresql://localhost:5432/platform"
    assert _redact("host=localhost dbname=platform") == "host=localhost dbname=platform"
