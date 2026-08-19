"""The `Ledger` contract, run against every backend there is.

One suite, parameterized over `SqliteLedger` and `PostgresLedger`. That is the
whole point of A3: the two are one design with and without contention, so a test
that passes on one and fails on the other is a bug by definition — and gate A7
(#18) is exactly this claim made about the full MVP scenario instead of about
methods.

Postgres is skipped unless `LEDGER_DSN` is set. When it is, the suite gets one
throwaway migrated database for the session and truncates between tests, so it
never touches the rig's own ledger.

Note on the vocabulary: *stored* timestamps are UTC ISO-8601 strings, because
`utc_now()` is the only thing that produces them. Storing a non-UTC offset is
outside the contract — Postgres would normalize it on the way back out and SQLite
would return it verbatim. *Querying* with one is inside the contract, and A7 found
out why it has to be: the comparison, not the spelling, is what a caller sees.
"""

import os
import sqlite3
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
    task_id_for,
)
from control_plane.store import LEDGER_DSN_ENV, SqliteLedger, session_key
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
            # memory_entries references bursts, so it comes along — TRUNCATE
            # refuses to leave a referencing table out (CASCADE would take
            # whatever else appeared later, which is not the same promise).
            conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries")
        backend = PostgresLedger(dsn)
    yield backend
    backend.close()


# The ids are derived (A6), so the suite derives them too — a test that types a
# task id by hand is testing a shape the platform no longer writes.
REPOSITORY = "owner/product"


def tid(issue_number=42):
    return task_id_for(REPOSITORY, issue_number)


def _task(issue_number=42, **kwargs):
    return TaskRecord(
        task_id=tid(issue_number),
        repository=REPOSITORY,
        issue_number=issue_number,
        **kwargs,
    )


def _burst(burst_id="b1", issue_number=42, **kwargs):
    return BurstRecord(burst_id=burst_id, task_id=tid(issue_number), **kwargs)


# --- tasks ---


def test_unknown_task_is_none(ledger):
    assert ledger.get_task("nope") is None


def test_task_roundtrip_preserves_every_field(ledger):
    task = _task(
        status=TaskStatus.AWAITING_REVIEW,
        session_id="sess-1",
        pr_number=7,
        attempts=2,
        review_rounds=1,
        next_burst_kind=BurstKind.REVIEW,
        last_review_id=99887766,
        last_burst_at="2026-08-14T09:15:30.123456+00:00",
    )
    ledger.upsert_task(task)

    loaded = ledger.get_task(tid())
    assert loaded is not None
    # updated_at is stamped by the write, so compare everything else verbatim
    assert {k: v for k, v in asdict(loaded).items() if k != "updated_at"} == {
        k: v for k, v in asdict(task).items() if k != "updated_at"
    }


def test_upsert_keeps_created_at_and_advances_updated_at(ledger):
    task = _task(created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
    ledger.upsert_task(task)
    first = ledger.get_task(tid())

    task.status = TaskStatus.WORKING
    ledger.upsert_task(task)
    second = ledger.get_task(tid())

    assert second.created_at == first.created_at == "2026-01-01T00:00:00+00:00"
    assert second.updated_at > first.updated_at
    assert second.status == TaskStatus.WORKING


def test_tasks_by_status_filters_and_orders_by_created_at(ledger):
    ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, created_at="2026-01-03T00:00:00+00:00"))
    ledger.upsert_task(_task(2, status=TaskStatus.WORKING, created_at="2026-01-01T00:00:00+00:00"))
    ledger.upsert_task(_task(3, status=TaskStatus.DONE, created_at="2026-01-02T00:00:00+00:00"))

    picked = ledger.tasks_by_status(TaskStatus.CLAIMED, TaskStatus.WORKING)
    assert [t.task_id for t in picked] == [tid(2), tid(1)]  # oldest first
    assert ledger.tasks_by_status(TaskStatus.BLOCKED) == []


def test_a_tie_in_created_at_is_broken_the_same_way(ledger):
    """Two tasks stamped in the same microsecond still come back in one order.

    `ORDER BY created_at` alone leaves the tie to the storage — index order under
    one plan, heap order under another — so the scheduler's "oldest first" would
    depend on the backend. Inserted here in descending id order, so returning them
    in insertion order fails. (A7: neither backend actually reordered these, which
    is exactly why the promise has to be in the query rather than observed once.)
    """
    same_instant = "2026-01-01T00:00:00+00:00"
    for issue in (3, 2, 1):
        ledger.upsert_task(_task(issue, status=TaskStatus.CLAIMED, created_at=same_instant))

    picked = ledger.tasks_by_status(TaskStatus.CLAIMED)
    assert [t.task_id for t in picked] == [tid(1), tid(2), tid(3)]


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
    burst.session_key = session_key(tid(), "b1")  # the snapshot it left (A5)
    burst.session_checksum = "c" * 64
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
    # two running bursts means two tasks: one per task is a wall on Postgres from
    # A4, and the scheduler never wanted more than that anyway
    ledger.upsert_task(_task(42))
    ledger.upsert_task(_task(43))
    ledger.record_burst(_burst("b1", issue_number=42))
    ledger.record_burst(_burst("b2", issue_number=43))
    assert {b.burst_id for b in ledger.running_bursts()} == {"b1", "b2"}

    done = ledger.get_burst("b1")
    done.status = BurstStatus.FAILED
    done.finished_at = "2026-08-14T09:05:00+00:00"
    done.exit_code = 1
    ledger.finish_burst(done)

    assert [b.burst_id for b in ledger.running_bursts()] == ["b2"]


def test_running_bursts_come_back_oldest_first(ledger):
    """Started second, inserted first — so insertion order is the wrong answer."""
    ledger.upsert_task(_task(42))
    ledger.upsert_task(_task(43))
    ledger.record_burst(_burst("later", issue_number=42, started_at="2026-08-14T09:00:02+00:00"))
    ledger.record_burst(_burst("earlier", issue_number=43, started_at="2026-08-14T09:00:01+00:00"))

    assert [b.burst_id for b in ledger.running_bursts()] == ["earlier", "later"]


def test_bursts_started_since_counts_the_window(ledger):
    ledger.upsert_task(_task(42))
    ledger.upsert_task(_task(43))
    ledger.record_burst(_burst("old", issue_number=42, started_at="2026-08-13T09:00:00+00:00"))
    ledger.record_burst(_burst("new", issue_number=43, started_at="2026-08-14T09:00:00+00:00"))

    assert ledger.bursts_started_since("2026-08-14T00:00:00+00:00") == 1
    assert ledger.bursts_started_since("2026-08-01T00:00:00+00:00") == 2
    assert ledger.bursts_started_since("2026-09-01T00:00:00+00:00") == 0


def test_the_window_starts_at_an_instant_not_at_a_clock_reading(ledger):
    """The same instant, spelled with three offsets, must count the same bursts.

    A7 measured what this guards: SQLite compares the text, so `+06:00` made the
    daily rail count 0 bursts where Postgres counted 2 — and `-08:00` made it count
    2 where Postgres counted 0. Both wrong in both directions, from one caller
    passing a stamp it had every right to pass.
    """
    ledger.upsert_task(_task(42))
    ledger.record_burst(_burst("b1", issue_number=42, started_at="2026-08-14T09:00:00+00:00"))

    # 08:00Z, spelled three ways — the burst is after all three
    for before in ("2026-08-14T08:00:00+00:00", "2026-08-14T14:00:00+06:00", "2026-08-14T00:00:00-08:00"):
        assert ledger.bursts_started_since(before) == 1, before
    # 10:00Z, spelled three ways — the burst is before all three
    for after in ("2026-08-14T10:00:00+00:00", "2026-08-14T16:00:00+06:00", "2026-08-14T02:00:00-08:00"):
        assert ledger.bursts_started_since(after) == 0, after


# --- the session tree ---


def test_lineage_is_recorded_at_spawn(ledger):
    """`resumed_from_burst_id` goes in with the insert, not with the result: it is
    a fact about what the burst was given, and it has to survive a crash that
    never reaches finalize."""
    ledger.upsert_task(_task())
    parent = _burst("b1")
    ledger.record_burst(parent)
    # the parent has to end before the child starts — one running burst per task
    parent.status = BurstStatus.SUCCEEDED
    parent.finished_at = "2026-08-14T09:05:00+00:00"
    ledger.finish_burst(parent)
    ledger.record_burst(_burst("b2", resumed_from_burst_id="b1"))

    assert ledger.get_burst("b1").resumed_from_burst_id is None
    assert ledger.get_burst("b2").resumed_from_burst_id == "b1"


def test_the_leaf_is_the_newest_burst_that_archived_something(ledger):
    """What `prepare_volume` asks for. Bursts that archive nothing — reviewer,
    tester, anything that crashed before harvest — are not nodes, so they can
    never become the leaf."""
    ledger.upsert_task(_task())
    assert ledger.latest_archived_burst(tid(42)) is None

    for burst_id, started, key in [
        ("b1", "2026-08-14T09:00:00+00:00", session_key(tid(), "b1")),
        ("b2", "2026-08-14T10:00:00+00:00", None),  # a reviewer: no snapshot
        ("b3", "2026-08-14T11:00:00+00:00", session_key(tid(), "b3")),
        ("b4", "2026-08-14T12:00:00+00:00", None),  # crashed before harvest
    ]:
        burst = _burst(burst_id, started_at=started, session_key=key, session_checksum="c" * 64)
        ledger.record_burst(burst)
        burst.status = BurstStatus.SUCCEEDED
        burst.finished_at = started
        ledger.finish_burst(burst)

    assert ledger.latest_archived_burst(tid(42)).burst_id == "b3"
    assert ledger.latest_archived_burst("nobody") is None


def test_the_leaf_is_per_task(ledger):
    """Two tasks in flight must not resume each other's sessions."""
    for issue_number in (42, 43):
        ledger.upsert_task(_task(issue_number))
        ledger.record_burst(
            _burst(
                f"b-{issue_number}",
                issue_number=issue_number,
                session_key=session_key(tid(issue_number), f"b-{issue_number}"),
                session_checksum="c" * 64,
            )
        )

    for issue_number in (42, 43):
        leaf = ledger.latest_archived_burst(tid(issue_number))
        assert leaf.session_key == session_key(tid(issue_number), f"b-{issue_number}")


def test_the_snapshot_is_written_by_finalize(ledger):
    """Harvest sets the key and checksum on the record, and `finish_burst` is what
    persists them — so a burst that never finalizes leaves no leaf, even if its
    bytes reached the archive."""
    ledger.upsert_task(_task())
    burst = _burst("b1")
    ledger.record_burst(burst)
    assert ledger.get_burst("b1").session_key is None

    burst.status = BurstStatus.SUCCEEDED
    burst.finished_at = "2026-08-14T09:05:00+00:00"
    burst.session_key = session_key(tid(), "b1")
    burst.session_checksum = "d" * 64
    ledger.finish_burst(burst)

    stored = ledger.get_burst("b1")
    assert (stored.session_key, stored.session_checksum) == (burst.session_key, "d" * 64)


# --- violations ---


def test_violations_by_task_and_in_full(ledger):
    ledger.record_violation(Violation(task_id=tid(42), burst_id="b1", tool="Read", reason="WALL"))
    ledger.record_violation(
        Violation(task_id=tid(42), burst_id="b1", tool="Bash", reason="WALL", detail="rm -rf /")
    )
    ledger.record_violation(Violation(task_id=tid(99), burst_id="b2", tool="Read", reason="WALL"))

    for_task = ledger.violations(tid(42))
    assert [v.tool for v in for_task] == ["Read", "Bash"]  # insertion order
    assert for_task[1].detail == "rm -rf /"
    assert len(ledger.violations()) == 3
    assert ledger.violations("nobody") == []


# --- the events outbox ---


def test_every_violation_announces_itself(ledger):
    """"Any new violations row is a page" (TO-BE §11) is implemented as an outbox
    row, written in the same breath as the violation: one CTE on Postgres, one
    transaction on SQLite, the same contract either way."""
    ledger.record_violation(Violation(task_id=tid(42), burst_id="b1", tool="Bash", reason="WALL"))

    events = ledger.events()
    assert [event.kind for event in events] == ["violation_recorded"]

    payload = dict(events[0].payload)
    # the id of the row it announces, so a consumer can read the rest of it
    assert isinstance(payload.pop("violation_id"), int)
    assert payload == {"task_id": tid(), "burst_id": "b1", "tool": "Bash", "reason": "WALL"}
    assert events[0].created_at  # an ISO string, like every other timestamp here


def test_the_cursor_is_the_consumers_own_position(ledger):
    """Read by cursor, at-least-once, replayable — no acknowledgement, no
    deletion, and no subscription. Each consumer keeps its own place."""
    for tool in ("Read", "Write", "Bash"):
        ledger.record_violation(Violation(task_id=tid(42), burst_id="b1", tool=tool, reason="WALL"))

    everything = ledger.events()
    assert [event.payload["tool"] for event in everything] == ["Read", "Write", "Bash"]
    assert [event.id for event in everything] == sorted(event.id for event in everything)

    after_first = ledger.events(after_id=everything[0].id)
    assert [event.id for event in after_first] == [event.id for event in everything[1:]]
    assert ledger.events(after_id=everything[-1].id) == []
    assert ledger.events() == everything  # replayable: reading it changed nothing


def test_a_violation_and_its_event_are_one_write(ledger):
    """Provoked rather than asserted: a violation the database refuses must leave
    no event behind, or the platform pages someone about a row that isn't there."""
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        ledger.record_violation(
            Violation(task_id=None, burst_id="b1", tool="Bash", reason="WALL")  # task_id is NOT NULL
        )

    assert ledger.violations() == []
    assert ledger.events() == []


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
    loaded = ledger.get_task(tid())
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
    task = ledger.get_task(tid())
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
        conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries")
    ledger = PostgresLedger(pg_dsn)
    ledger.upsert_task(_task())

    ledger._conn.close()  # as if Postgres restarted under us

    ledger.upsert_task(_task(status=TaskStatus.WORKING))
    assert ledger.get_task(tid()).status == TaskStatus.WORKING
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
