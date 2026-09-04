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
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.models import (
    BurstKind,
    BurstRecord,
    BurstStatus,
    RepositoryRecord,
    Role,
    TaskRecord,
    TaskStatus,
    Violation,
    task_id_for,
    utc_now,
)
from control_plane.store import LEDGER_DSN_ENV, RegistryError, SqliteLedger, session_key
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
    """A ledger of each backend in turn, holding one registered product."""
    if request.param == "sqlite":
        backend = SqliteLedger(tmp_path / "ledger.sqlite3")
    else:
        from control_plane.postgres import PostgresLedger

        dsn = request.getfixturevalue("pg_dsn")  # lazily: sqlite runs need no database
        with psycopg.connect(dsn, autocommit=True) as conn:
            # memory_entries references bursts, so it comes along — TRUNCATE
            # refuses to leave a referencing table out (CASCADE would take
            # whatever else appeared later, which is not the same promise).
            conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries, repositories, audit")
            # roles cannot be truncated (frozen pipeline rows, D5) — reset the
            # open fields to their seed so a `role set` never outlives its test
            conn.execute(
                "UPDATE roles SET prompt_preamble = '', model = 'claude-sonnet-5', max_budget_usd = 2.00, "
                "max_turns = 100, mount_mode = CASE WHEN role = 'dev' THEN 'rw' ELSE 'ro' END "
                "WHERE kind = 'pipeline'"
            )
        backend = PostgresLedger(dsn)
    # The registry wall (C1): a task names a registered product, so the suite's
    # product exists before any test inserts one — registered through the verb,
    # exactly as an operator would. Every backend's event stream therefore opens
    # with `product_registered`, which the outbox tests account for.
    backend.register_repository(
        RepositoryRecord(repository=REPOSITORY, team="owners"), actor="contract-fixture"
    )
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


# --- leases and compare-and-set (E1) ---
#
# The scheduler's writes, as STORES.md's single-statement shapes. Each one is
# tested through its rowcount semantics rather than by racing two connections —
# the race is E2's test; what belongs here is that both backends answer a stale
# expectation, a foreign holder and a lapsed lease the same way.

REPLICA = "host-a:100"
PEER = "host-b:200"
LONG_AGO = "2020-01-01T00:00:00+00:00"
FAR_AHEAD = "2099-01-01T00:00:00+00:00"


def _instant(iso):
    return datetime.fromisoformat(iso)


def test_create_task_inserts_once_and_never_overwrites(ledger):
    assert ledger.create_task(_task(status=TaskStatus.CLAIMED, next_burst_kind="work")) is True
    assert ledger.create_task(_task(status=TaskStatus.BLOCKED)) is False  # a peer got there first
    kept = ledger.get_task(tid())
    assert (kept.status, kept.next_burst_kind) == (TaskStatus.CLAIMED, "work")


def test_transition_is_compare_and_set_on_status(ledger):
    ledger.upsert_task(_task(status=TaskStatus.CLAIMED, created_at=LONG_AGO, updated_at=LONG_AGO))

    moved = ledger.transition_task(
        tid(), {"status": TaskStatus.WORKING, "last_burst_at": "2026-08-14T09:00:00+00:00"},
        expected_status=TaskStatus.CLAIMED, expected_holder=None,
    )
    assert moved is not None
    assert (moved.status, moved.last_burst_at) == (TaskStatus.WORKING, "2026-08-14T09:00:00+00:00")
    assert moved.created_at == LONG_AGO and moved.updated_at > LONG_AGO  # the row as written

    # the same expectation again is stale now: nothing written, None returned
    assert ledger.transition_task(
        tid(), {"status": TaskStatus.BLOCKED}, expected_status=TaskStatus.CLAIMED, expected_holder=None
    ) is None
    assert ledger.get_task(tid()).status == TaskStatus.WORKING


def test_transition_guards_the_holder_null_safely(ledger):
    ledger.upsert_task(_task(status=TaskStatus.WORKING, claimed_by=REPLICA, claimed_until=FAR_AHEAD))
    # the wrong holder — and "nobody" — both miss a held row
    for holder in (PEER, None):
        assert ledger.transition_task(
            tid(), {"status": TaskStatus.CLAIMED}, expected_status=TaskStatus.WORKING, expected_holder=holder
        ) is None
    moved = ledger.transition_task(
        tid(), {"status": TaskStatus.CLAIMED, "claimed_by": None, "claimed_until": None},
        expected_status=TaskStatus.WORKING, expected_holder=REPLICA,
    )
    assert moved is not None and moved.claimed_by is None and moved.claimed_until is None
    # and an unheld row is matched by "nobody", never by a name
    assert ledger.transition_task(
        tid(), {"attempts": 1}, expected_status=TaskStatus.CLAIMED, expected_holder=REPLICA
    ) is None
    assert ledger.transition_task(
        tid(), {"attempts": 1}, expected_status=TaskStatus.CLAIMED, expected_holder=None
    ).attempts == 1


def test_transition_writes_only_the_columns_it_names(ledger):
    ledger.upsert_task(_task(status=TaskStatus.WORKING, pr_number=7, attempts=2, session_id="sess-1"))
    moved = ledger.transition_task(
        tid(), {"status": TaskStatus.AWAITING_REVIEW}, expected_status=TaskStatus.WORKING, expected_holder=None
    )
    assert (moved.pr_number, moved.attempts, moved.session_id) == (7, 2, "sess-1")


@pytest.mark.parametrize(
    "changes",
    [{}, {"task_id": "x"}, {"created_at": LONG_AGO}, {"repository": "other/one"}, {"statuss": "typo"}],
)
def test_transition_refuses_what_is_not_a_transition(ledger, changes):
    ledger.upsert_task(_task(status=TaskStatus.CLAIMED))
    with pytest.raises(ValueError):
        ledger.transition_task(tid(), changes, expected_status=TaskStatus.CLAIMED, expected_holder=None)


def _claim(ledger, replica=REPLICA, **overrides):
    # concurrency wide open by default: the tests below race two replicas onto
    # two rows on purpose; the one about the cap sets it to 1 itself
    params = dict(ceiling=20, since=LONG_AGO, lease_s=90, cooldown_s=300, concurrency=20) | overrides
    return ledger.claim_next(REPOSITORY, replica, **params)


def test_claim_leases_the_oldest_eligible_task_to_the_caller(ledger):
    ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, created_at="2026-01-02T00:00:00+00:00"))
    ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED, created_at="2026-01-01T00:00:00+00:00"))
    ledger.upsert_task(_task(3, status=TaskStatus.AWAITING_REVIEW, created_at=LONG_AGO))

    first = _claim(ledger)
    assert first.task_id == tid(2)  # oldest claimed; awaiting_review is not queued work
    assert first.claimed_by == REPLICA
    assert _instant(first.claimed_until) > datetime.now(UTC)
    assert first.status == TaskStatus.CLAIMED  # the lease is not the transition
    assert ledger.get_task(tid(2)).claimed_by == REPLICA  # written, not just returned

    second = _claim(ledger, replica=PEER)
    assert second.task_id == tid(1)  # the leased row is skipped, the next one taken
    assert _claim(ledger, replica="host-c:300") is None  # nothing left to lease


def test_a_live_lease_is_skipped_and_a_lapsed_one_is_reclaimed(ledger):
    ledger.upsert_task(_task(status=TaskStatus.CLAIMED, claimed_by=PEER, claimed_until=FAR_AHEAD))
    assert _claim(ledger) is None

    ledger.upsert_task(_task(status=TaskStatus.CLAIMED, claimed_by=PEER, claimed_until=LONG_AGO))
    reclaimed = _claim(ledger)
    assert reclaimed.claimed_by == REPLICA  # a lapsed lease is anyone's — visible data, never a held lock
    assert _instant(reclaimed.claimed_until) > datetime.now(UTC)


def test_the_failure_cooldown_is_the_claims_where_clause(ledger):
    ledger.upsert_task(_task(status=TaskStatus.CLAIMED, attempts=1, last_burst_at=utc_now()))
    assert _claim(ledger, cooldown_s=3600) is None  # failed a moment ago
    assert _claim(ledger, cooldown_s=0) is not None  # cooled

    ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED, attempts=1, last_burst_at=LONG_AGO))
    assert _claim(ledger, replica=PEER, cooldown_s=3600).task_id == tid(2)  # failed long ago


def test_the_daily_rail_counts_admitted_work_not_recorded_bursts(ledger):
    ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED))
    ledger.upsert_task(_task(3, status=TaskStatus.DONE))
    ledger.record_burst(_burst("b-old", issue_number=3, started_at="2026-08-01T00:00:00+00:00"))

    # a burst started before the window is not today's
    assert _claim(ledger, ceiling=1, since="2026-08-02T00:00:00+00:00").task_id == tid(1)
    # that claim is a spawn in flight — a live lease with no burst row yet — and
    # counts: under a ceiling of 1 the second replica is refused, not doubled
    assert _claim(ledger, replica=PEER, ceiling=1, since="2026-08-02T00:00:00+00:00") is None
    # once the burst row exists the lease and the row are one admission, not two
    ledger.record_burst(_burst("b-new", issue_number=1))
    assert _claim(ledger, replica=PEER, ceiling=2, since="2026-08-02T00:00:00+00:00").task_id == tid(2)
    assert _claim(ledger, replica="host-c:300", ceiling=3, since="2026-08-02T00:00:00+00:00") is None


def test_the_concurrency_rail_counts_the_products_work_in_flight(ledger):
    """E2: the row's `concurrency` is the number of the product's bursts in
    flight at once, fleet-wide — a live lease or a running burst, whichever
    stage a spawn is at — so with the default of 1, a second replica stands by
    rather than opening a second lane."""
    ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED))

    assert _claim(ledger, concurrency=1).task_id == tid(1)
    assert _claim(ledger, replica=PEER, concurrency=1) is None  # the lease is in flight
    assert _claim(ledger, replica=PEER, concurrency=2).task_id == tid(2)  # a wider row, a second lane

    # a running burst counts even once its lease has lapsed — the reconciler,
    # not admission, decides whether that work is dead
    ledger.upsert_task(_task(1, status=TaskStatus.WORKING, claimed_by=REPLICA, claimed_until=LONG_AGO))
    ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED))
    ledger.record_burst(_burst("b1", issue_number=1))
    assert _claim(ledger, replica="host-c:300", concurrency=1) is None
    # released and finished: nothing in flight, the lane is open again
    ledger.upsert_task(_task(1, status=TaskStatus.AWAITING_REVIEW))
    burst = ledger.get_burst("b1")
    burst.status, burst.finished_at = BurstStatus.SUCCEEDED, utc_now()
    ledger.finish_burst(burst)
    assert _claim(ledger, replica="host-c:300", concurrency=1).task_id == tid(2)


def test_renewal_is_the_holders_alone(ledger):
    ledger.upsert_task(_task(status=TaskStatus.CLAIMED))
    leased = _claim(ledger)
    before = _instant(leased.claimed_until)

    assert ledger.renew_lease(tid(), PEER, lease_s=3600) is False  # not the holder
    assert _instant(ledger.get_task(tid()).claimed_until) == before

    assert ledger.renew_lease(tid(), REPLICA, lease_s=3600) is True
    assert _instant(ledger.get_task(tid()).claimed_until) > before


def test_orphaned_means_no_live_lease(ledger):
    """What the reconciler may touch: rows whose lease lapsed or never existed.
    A live lease — whoever holds it — is proof of life from any host."""
    ledger.upsert_task(_task(1, status=TaskStatus.WORKING, claimed_by=PEER, claimed_until=FAR_AHEAD))
    ledger.upsert_task(_task(2, status=TaskStatus.WORKING, claimed_by=PEER, claimed_until=LONG_AGO))
    ledger.upsert_task(_task(3, status=TaskStatus.WORKING))  # pre-E1 shape: never leased
    for issue in (1, 2, 3):
        ledger.record_burst(_burst(f"b{issue}", issue_number=issue))

    working = ledger.tasks_by_status(TaskStatus.WORKING, repository=REPOSITORY)
    assert [t.issue_number for t in working] == [1, 2, 3]
    orphaned = ledger.tasks_by_status(TaskStatus.WORKING, repository=REPOSITORY, orphaned=True)
    assert [t.issue_number for t in orphaned] == [2, 3]

    assert [b.burst_id for b in ledger.running_bursts(repository=REPOSITORY)] == ["b1", "b2", "b3"]
    assert [b.burst_id for b in ledger.running_bursts(repository=REPOSITORY, orphaned=True)] == ["b2", "b3"]
    assert [b.burst_id for b in ledger.running_bursts(orphaned=True)] == ["b2", "b3"]


def test_no_call_leaves_a_transaction_open(ledger):
    """The E1 done-when, at the store: every write shape returns with the
    connection idle. `in_transaction` is the seam; the tick-level assertion —
    idle at every I/O point — is tests/test_transactions.py."""
    def idle(result=None):
        assert ledger.in_transaction is False
        return result

    idle(ledger.upsert_task(_task(status=TaskStatus.CLAIMED)))
    idle(ledger.create_task(_task(2, status=TaskStatus.CLAIMED)))
    leased = idle(_claim(ledger))
    idle(ledger.renew_lease(leased.task_id, REPLICA, 90))
    idle(ledger.transition_task(
        leased.task_id, {"status": TaskStatus.WORKING}, expected_status=TaskStatus.CLAIMED, expected_holder=REPLICA
    ))
    idle(ledger.transition_task(  # the losing branch too
        leased.task_id, {"status": TaskStatus.WORKING}, expected_status=TaskStatus.CLAIMED, expected_holder=REPLICA
    ))
    burst = _burst(issue_number=leased.issue_number)
    idle(ledger.record_burst(burst))
    burst.status, burst.finished_at = BurstStatus.SUCCEEDED, utc_now()
    idle(ledger.finish_burst(burst))
    idle(ledger.finish_burst(burst))  # the refused finalize
    idle(ledger.record_violation(Violation(task_id=leased.task_id, burst_id="b1", tool="Bash", reason="r")))
    idle(ledger.touch_repository_burst(REPOSITORY, utc_now()))
    idle(ledger.claim_observation(REPOSITORY, REPLICA, 60))
    idle(ledger.claim_observation(REPOSITORY, PEER, 60))  # the refused claim too
    idle(ledger.update_repository(REPOSITORY, {"team": "others"}, actor="t"))
    idle(ledger.set_repository_state(REPOSITORY, "paused", actor="t", reason="because"))
    with pytest.raises(RegistryError):
        ledger.set_repository_state(REPOSITORY, "paused", actor="t", reason="again")
    idle()
    idle(ledger.update_role("dev", {"max_turns": 7}, actor="t"))
    idle(ledger.tasks_by_status(TaskStatus.WORKING, orphaned=True))
    idle(ledger.running_bursts(orphaned=True))
    idle(ledger.bursts_started_since(LONG_AGO))


# --- bursts ---


def test_burst_roundtrip_and_finalize(ledger):
    ledger.upsert_task(_task())
    burst = _burst(
        trace_id="0" * 32,
        span_id="0" * 16,
        model="claude-sonnet-5",
        started_at="2026-08-14T09:00:00+00:00",
    )
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


def test_the_trace_pair_goes_in_at_spawn_and_is_empty_when_there_was_none(ledger):
    """Both halves, and both as strings. A successor's span link names a *span*, so
    `trace_id` alone would only buy a link with a zero span id — which resolves to
    nothing in a viewer (B1). The absent case is `""` rather than NULL on purpose:
    `telemetry.link_to_burst` decides by truthiness, and a caller that has to handle
    both an empty string and a None is a caller that will handle one of them."""
    ledger.upsert_task(_task())
    ledger.upsert_task(_task(43))  # a second task: one running burst per task
    ledger.record_burst(_burst("b1", trace_id="a" * 32, span_id="b" * 16))
    ledger.record_burst(_burst("b2", issue_number=43))  # tracing produced no span

    recorded = ledger.get_burst("b1")
    assert (recorded.trace_id, recorded.span_id) == ("a" * 32, "b" * 16)
    untraced = ledger.get_burst("b2")
    assert (untraced.trace_id, untraced.span_id) == ("", "")


def test_the_leaf_is_the_newest_burst_that_archived_something(ledger):
    """What `prepare_volume` asks for. Bursts that archive nothing — a reviewer,
    anything that crashed before harvest — are not nodes, so they can never
    become the leaf."""
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


def test_an_archived_tester_burst_never_becomes_the_leaf(ledger):
    """Leafhood is the dev's (B5). Tester volumes are archived as *evidence* —
    snapshot, key, checksum, everything a node has — so the only thing standing
    between an archived tester burst and the next dev burst resuming the tester's
    session is the role filter in this query. The newest snapshot here is the
    tester's; the leaf must still be the dev's — and a task whose only snapshots
    are a tester's has no leaf at all."""
    ledger.upsert_task(_task())
    for burst_id, started, role, kind in [
        ("b-dev", "2026-08-14T09:00:00+00:00", Role.DEV, BurstKind.WORK),
        ("b-test", "2026-08-14T10:00:00+00:00", Role.TESTER, BurstKind.TEST),
    ]:
        burst = _burst(
            burst_id,
            started_at=started,
            role=role,
            kind=kind,
            session_key=session_key(tid(), burst_id),
            session_checksum="c" * 64,
        )
        ledger.record_burst(burst)
        burst.status = BurstStatus.SUCCEEDED
        burst.finished_at = started
        ledger.finish_burst(burst)

    assert ledger.latest_archived_burst(tid(42)).burst_id == "b-dev"

    ledger.upsert_task(_task(43))  # only a tester's snapshot: evidence, no leaf
    ledger.record_burst(
        _burst(
            "b-test-only",
            issue_number=43,
            role=Role.TESTER,
            kind=BurstKind.TEST,
            session_key=session_key(tid(43), "b-test-only"),
            session_checksum="c" * 64,
        )
    )
    assert ledger.latest_archived_burst(tid(43)) is None


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

    # the stream opens with the fixture's registration — the registry's own
    # announcements share the outbox (C1)
    events = ledger.events()
    assert [event.kind for event in events] == ["product_registered", "violation_recorded"]

    payload = dict(events[-1].payload)
    # the id of the row it announces, so a consumer can read the rest of it
    assert isinstance(payload.pop("violation_id"), int)
    assert payload == {"task_id": tid(), "burst_id": "b1", "tool": "Bash", "reason": "WALL"}
    assert events[-1].created_at  # an ISO string, like every other timestamp here


def test_the_cursor_is_the_consumers_own_position(ledger):
    """Read by cursor, at-least-once, replayable — no acknowledgement, no
    deletion, and no subscription. Each consumer keeps its own place."""
    for tool in ("Read", "Write", "Bash"):
        ledger.record_violation(Violation(task_id=tid(42), burst_id="b1", tool=tool, reason="WALL"))

    everything = ledger.events()
    # .get: the first event is the fixture's product_registered, which has no tool
    assert [event.payload.get("tool") for event in everything] == [None, "Read", "Write", "Bash"]
    assert [event.id for event in everything] == sorted(event.id for event in everything)

    after_first = ledger.events(after_id=everything[0].id)
    assert [event.id for event in after_first] == [event.id for event in everything[1:]]
    assert ledger.events(after_id=everything[-1].id) == []
    assert ledger.events() == everything  # replayable: reading it changed nothing


def test_a_violation_and_its_event_are_one_write(ledger):
    """Provoked rather than asserted: a violation the database refuses must leave
    no event behind, or the platform pages someone about a row that isn't there."""
    before = ledger.events()  # the fixture's registration, and nothing else
    with pytest.raises((sqlite3.IntegrityError, psycopg.IntegrityError)):
        ledger.record_violation(
            Violation(task_id=None, burst_id="b1", tool="Bash", reason="WALL")  # task_id is NOT NULL
        )

    assert ledger.violations() == []
    assert ledger.events() == before


# --- the product registry (C1) ---


PRODUCT = "owner/registered"  # the suite's own product; REPOSITORY is the fixture's


def _product(repository=PRODUCT, **kwargs):
    kwargs.setdefault("team", "bots")
    return RepositoryRecord(repository=repository, **kwargs)


def test_a_registration_is_the_row_the_operator_declared(ledger):
    """Materialized, not inherited: every column comes back exactly as declared —
    booleans as booleans, the refs map as a map, the share as a float."""
    row = _product(
        review_enabled=True,
        review_provider="claude,copilot",
        e2e_enabled=True,
        test_env_refs={"DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN", "GUILD_ID": "env:TEST_GUILD_ID"},
        concurrency=2,
        daily_bursts=7,
        usage_share=0.5,
    )
    ledger.register_repository(row, actor="maksim", reason="the second product")

    loaded = ledger.get_repository(PRODUCT)
    assert loaded is not None
    assert asdict(loaded) == asdict(row)
    assert loaded.review_enabled is True and loaded.e2e_enabled is True
    assert isinstance(loaded.usage_share, float)
    assert ledger.get_repository("ghost/none") is None


def test_products_list_in_one_order(ledger):
    for slug in ("zzz/last", "aaa/first"):
        ledger.register_repository(_product(repository=slug), "op")
    assert [r.repository for r in ledger.list_repositories()] == ["aaa/first", REPOSITORY, "zzz/last"]


def test_registering_twice_is_refused_and_leaves_no_trail(ledger):
    ledger.register_repository(_product(), actor="op")
    with pytest.raises(RegistryError, match="already registered"):
        ledger.register_repository(_product(team="other"), actor="op")
    assert len(ledger.audit_trail(PRODUCT)) == 1
    assert ledger.get_repository(PRODUCT).team == "bots"  # the first declaration stands


def test_the_audit_trail_is_queryable_from_the_first_verb(ledger):
    """C1's done-when, verbatim. actor, action, target, reason — TO-BE §2."""
    ledger.register_repository(_product(), actor="maksim", reason="onboarding")

    entry = ledger.audit_trail(PRODUCT)[-1]
    assert (entry.actor, entry.action, entry.target, entry.reason) == (
        "maksim", "product.register", PRODUCT, "onboarding",
    )
    assert isinstance(entry.id, int) and entry.at  # a cursor and an ISO stamp

    everything = ledger.audit_trail()  # unfiltered: the fixture's registration first
    assert [e.target for e in everything] == [REPOSITORY, PRODUCT]
    assert [e.id for e in everything] == sorted(e.id for e in everything)


def test_every_registry_mutation_announces_itself(ledger):
    """Mutation, audit row, outbox event — one write, each verb (C1). The payload
    names its audit row, so a consumer can read the who/why without guessing."""
    ledger.register_repository(_product(), actor="op")
    ledger.set_repository_state(PRODUCT, "paused", "op", "rate limited")
    ledger.set_repository_state(PRODUCT, "active", "op")
    ledger.update_repository(PRODUCT, {"daily_bursts": 9}, "op", "quota bump")

    events = [e for e in ledger.events() if e.kind.startswith("product_")]
    assert [e.kind for e in events] == [
        "product_registered",  # the fixture's
        "product_registered",
        "product_paused",
        "product_resumed",
        "product_updated",
    ]
    updated = events[-1]
    assert updated.payload["repository"] == PRODUCT
    assert updated.payload["actor"] == "op"
    assert updated.payload["changed"] == ["daily_bursts"]
    audit_ids = {entry.id for entry in ledger.audit_trail()}
    assert all(event.payload["audit_id"] in audit_ids for event in events)


def test_pause_and_resume_flip_state_exactly_once(ledger):
    """The transition is a CAS: a no-op flip is refused, and a refusal leaves no
    audit row and no event — the trail records what happened, never what didn't."""
    ledger.register_repository(_product(), "op")
    ledger.set_repository_state(PRODUCT, "paused", "op", "incident")
    assert ledger.get_repository(PRODUCT).state == "paused"
    with pytest.raises(RegistryError, match="already paused"):
        ledger.set_repository_state(PRODUCT, "paused", "op", "again")

    ledger.set_repository_state(PRODUCT, "active", "op")
    assert ledger.get_repository(PRODUCT).state == "active"
    with pytest.raises(RegistryError, match="already active"):
        ledger.set_repository_state(PRODUCT, "active", "op")

    assert [a.action for a in ledger.audit_trail(PRODUCT)] == [
        "product.register", "product.pause", "product.resume",
    ]


def test_pausing_needs_a_reason(ledger):
    ledger.register_repository(_product(), "op")
    with pytest.raises(RegistryError, match="reason"):
        ledger.set_repository_state(PRODUCT, "paused", "op", "  ")
    assert ledger.get_repository(PRODUCT).state == "active"


def test_a_state_the_registry_does_not_speak_is_refused(ledger):
    ledger.register_repository(_product(), "op")
    with pytest.raises(RegistryError, match="neither"):
        ledger.set_repository_state(PRODUCT, "retired", "op", "sunset")


def test_the_unregistered_cannot_be_mutated(ledger):
    with pytest.raises(RegistryError, match="not registered"):
        ledger.set_repository_state("ghost/none", "paused", "op", "reason")
    with pytest.raises(RegistryError, match="not registered"):
        ledger.update_repository("ghost/none", {"team": "x"}, "op")


def test_set_touches_only_the_operator_columns(ledger):
    ledger.register_repository(_product(), "op")
    with pytest.raises(RegistryError, match="not settable"):
        ledger.update_repository(PRODUCT, {"state": "paused"}, "op")  # pause/resume own state
    with pytest.raises(RegistryError, match="nothing to set"):
        ledger.update_repository(PRODUCT, {}, "op")
    assert [a.action for a in ledger.audit_trail(PRODUCT)] == ["product.register"]


def test_set_changes_the_row_and_bumps_updated_at(ledger):
    ledger.register_repository(_product(), "op")
    before = ledger.get_repository(PRODUCT)

    changes = {
        "team": "platform",
        "review_enabled": True,
        "review_provider": "claude",
        "e2e_enabled": True,
        "test_env_refs": {"TOKEN": "env:TEST_TOKEN"},
        "concurrency": 3,
        "daily_bursts": 11,
        "usage_share": 0.25,
    }
    ledger.update_repository(PRODUCT, changes, "op", "declared for real")

    after = ledger.get_repository(PRODUCT)
    for column, value in changes.items():
        assert getattr(after, column) == value
    assert after.updated_at > before.updated_at
    assert after.registered_at == before.registered_at
    assert after.state == before.state  # set never touches it


# --- roles (D5) ---
#
# The same rows on both backends: seeded by 0002 on Postgres, by _SCHEMA on
# SQLite, edited by one verb whose refusals read identically.


def test_the_pipeline_roles_are_seeded_identically(ledger):
    rows = {row.role: row for row in ledger.list_roles()}
    assert set(rows) == {"dev", "reviewer", "tester", "monitor"}
    assert {row.kind for row in rows.values()} == {"pipeline"}
    assert {row.identity for row in rows.values()} == {"none"}
    assert all(row.tools == ["*"] for row in rows.values())
    assert rows["dev"].mount_mode == "rw" and rows["reviewer"].mount_mode == "ro"
    assert rows["monitor"].sink == "event" and rows["dev"].sink == "issue_comment"
    assert (rows["dev"].model, rows["dev"].max_budget_usd, rows["dev"].max_turns) == ("claude-sonnet-5", 2.0, 100)
    assert isinstance(rows["dev"].max_budget_usd, float)  # numeric → float at the edge
    assert rows["dev"].trigger is None
    assert ledger.get_role("ghost") is None


def test_role_set_changes_the_open_fields_and_leaves_a_trail(ledger):
    before = ledger.get_role("reviewer")
    changes = {
        "prompt_preamble": "Be terse.",
        "model": "claude-opus-5",
        "max_budget_usd": 0.5,
        "max_turns": 30,
        "mount_mode": "rw",
    }
    ledger.update_role("reviewer", changes, "op", "cheaper reviews")

    after = ledger.get_role("reviewer")
    for column, value in changes.items():
        assert getattr(after, column) == value
    assert after.updated_at > before.updated_at
    assert after.created_at == before.created_at
    assert (after.kind, after.identity, after.sink, after.tools) == ("pipeline", "none", "issue_comment", ["*"])

    (entry,) = ledger.audit_trail("reviewer")
    assert (entry.actor, entry.action, entry.reason) == ("op", "role.set", "cheaper reviews")
    (event,) = [e for e in ledger.events() if e.kind == "role_updated"]
    assert event.payload == {
        "audit_id": entry.id,
        "role": "reviewer",
        "actor": "op",
        "changed": sorted(changes),
    }


def test_role_set_refuses_walls_unknowns_and_ghosts(ledger):
    from control_plane.roles import RoleError

    with pytest.raises(RoleError, match="frozen: tools"):
        ledger.update_role("dev", {"tools": ["Bash"], "model": "x"}, "op")
    with pytest.raises(RoleError, match="not settable: trigger"):
        ledger.update_role("dev", {"trigger": {"schedule": "daily"}}, "op")
    with pytest.raises(RoleError, match="no role named 'ghost'"):
        ledger.update_role("ghost", {"model": "x"}, "op")
    # nothing was written on any refusal — no row change, no trail
    assert ledger.get_role("dev").model == "claude-sonnet-5"
    assert ledger.audit_trail("dev") == [] and ledger.audit_trail("ghost") == []


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


@needs_postgres
def test_a_locked_row_is_skipped_not_waited_for(pg_dsn):
    """FOR UPDATE SKIP LOCKED, measured: a peer mid-statement on task 1 (here, a
    transaction holding its row lock) costs this replica nothing — it takes
    task 2 and returns at once. Without SKIP LOCKED the claim would block on
    the peer's lock, which is exactly the "think-time wait" the discipline
    forbids."""
    from control_plane.postgres import PostgresLedger

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries, repositories, audit")
    ledger = PostgresLedger(pg_dsn)
    ledger.register_repository(RepositoryRecord(repository=REPOSITORY, team="owners"), actor="t")
    ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED, created_at="2026-01-01T00:00:00+00:00"))
    ledger.upsert_task(_task(2, status=TaskStatus.CLAIMED, created_at="2026-01-02T00:00:00+00:00"))
    try:
        with psycopg.connect(pg_dsn) as peer:  # a transaction, deliberately — the peer's row lock
            peer.execute("SELECT task_id FROM tasks WHERE task_id = %s FOR UPDATE", (tid(1),))
            claimed = _claim(ledger)
            assert claimed.task_id == tid(2)  # the older row is locked; skipped, not waited for
            peer.rollback()
        assert _claim(ledger, replica=PEER).task_id == tid(1)  # free again
    finally:
        ledger.close()


@needs_postgres
def test_the_admission_fence_is_try_only(pg_dsn):
    """A peer inside admission holds the advisory lock; this replica idles the
    tick rather than queue behind it — and claims nothing, because the count it
    would have compared against is the peer's to change right now."""
    from control_plane.postgres import ADMISSION_LOCK_KEY, PostgresLedger

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries, repositories, audit")
    ledger = PostgresLedger(pg_dsn)
    ledger.register_repository(RepositoryRecord(repository=REPOSITORY, team="owners"), actor="t")
    ledger.upsert_task(_task(status=TaskStatus.CLAIMED))
    try:
        with psycopg.connect(pg_dsn, autocommit=True) as peer:
            assert peer.execute("SELECT pg_try_advisory_lock(%s)", (ADMISSION_LOCK_KEY,)).fetchone()[0]
            assert _claim(ledger) is None  # fenced out, instantly
            assert ledger.get_task(tid()).claimed_by is None
            peer.execute("SELECT pg_advisory_unlock(%s)", (ADMISSION_LOCK_KEY,))
        assert _claim(ledger).claimed_by == REPLICA
        # and this replica released its own hold on the way out: a peer can take it
        with psycopg.connect(pg_dsn, autocommit=True) as peer:
            assert peer.execute("SELECT pg_try_advisory_lock(%s)", (ADMISSION_LOCK_KEY,)).fetchone()[0]
    finally:
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


# --- C4: per-product scoping — a product's scheduler sees only its own rows ---

NEIGHBOR = "owner/neighbor"


def _neighbor_task(issue_number=7, **kwargs):
    return TaskRecord(
        task_id=task_id_for(NEIGHBOR, issue_number),
        repository=NEIGHBOR,
        issue_number=issue_number,
        **kwargs,
    )


def _with_neighbor(ledger):
    ledger.register_repository(_product(repository=NEIGHBOR), "op")


def test_tasks_by_status_scopes_to_one_product(ledger):
    _with_neighbor(ledger)
    ledger.upsert_task(_task(1, status=TaskStatus.CLAIMED))
    ledger.upsert_task(_neighbor_task(2, status=TaskStatus.CLAIMED))

    both = ledger.tasks_by_status(TaskStatus.CLAIMED)
    assert {t.repository for t in both} == {REPOSITORY, NEIGHBOR}
    assert [t.repository for t in ledger.tasks_by_status(TaskStatus.CLAIMED, repository=REPOSITORY)] == [
        REPOSITORY
    ]
    assert ledger.tasks_by_status(TaskStatus.CLAIMED, repository="ghost/none") == []


def test_running_bursts_scope_through_the_task(ledger):
    """A burst carries no repository column — the scope rides the join to its task."""
    _with_neighbor(ledger)
    ledger.upsert_task(_task(1))
    ledger.upsert_task(_neighbor_task(2))
    ledger.record_burst(_burst("b-ours", issue_number=1))
    ledger.record_burst(BurstRecord(burst_id="b-theirs", task_id=task_id_for(NEIGHBOR, 2)))

    assert {b.burst_id for b in ledger.running_bursts()} == {"b-ours", "b-theirs"}
    assert [b.burst_id for b in ledger.running_bursts(repository=NEIGHBOR)] == ["b-theirs"]


def test_the_daily_count_scopes_to_one_product(ledger):
    """The per-product quota (C4): a neighbor's spend never counts against ours."""
    _with_neighbor(ledger)
    ledger.upsert_task(_task(1))
    ledger.upsert_task(_neighbor_task(2))
    ledger.record_burst(_burst("b-ours", issue_number=1, started_at="2026-09-02T10:00:00+00:00"))
    # finished, not running — one *running* burst per task is a wall (A4)
    for n in range(3):
        ledger.record_burst(
            BurstRecord(
                burst_id=f"b-theirs-{n}",
                task_id=task_id_for(NEIGHBOR, 2),
                status=BurstStatus.SUCCEEDED,
                started_at=f"2026-09-02T10:00:0{n + 1}+00:00",
                finished_at=f"2026-09-02T10:00:0{n + 2}+00:00",
            )
        )

    since = "2026-09-02T00:00:00+00:00"
    assert ledger.bursts_started_since(since) == 4
    assert ledger.bursts_started_since(since, repository=REPOSITORY) == 1
    assert ledger.bursts_started_since(since, repository=NEIGHBOR) == 3


def test_touch_stamps_the_lrs_cursor_and_nothing_else(ledger):
    """`touch_repository_burst` is operational state, not an operator mutation:
    the stamp lands, `updated_at` stays, no audit row, no event."""
    before = ledger.get_repository(REPOSITORY)
    audit_before = len(ledger.audit_trail())
    events_before = len(ledger.events())
    at = utc_now()

    ledger.touch_repository_burst(REPOSITORY, at)

    after = ledger.get_repository(REPOSITORY)
    assert after.last_burst_at == at
    assert after.updated_at == before.updated_at
    assert len(ledger.audit_trail()) == audit_before
    assert len(ledger.events()) == events_before


def test_touch_of_the_unregistered_is_a_quiet_no_op(ledger):
    ledger.touch_repository_burst("ghost/none", utc_now())  # logs a warning, raises nothing
    assert ledger.get_repository("ghost/none") is None


def test_the_observation_lease_is_the_task_lease_on_the_registry_row(ledger):
    """E2: whoever's claim lands observes the product for the interval; a
    peer's live lease refuses, a lapsed one is anyone's. Operational state like
    the LRS stamp — no audit row, no event, `updated_at` untouched."""
    before = ledger.get_repository(REPOSITORY)
    audit_before, events_before = len(ledger.audit_trail()), len(ledger.events())

    assert ledger.claim_observation(REPOSITORY, REPLICA, 60) is True
    row = ledger.get_repository(REPOSITORY)
    assert row.claimed_by == REPLICA and _instant(row.claimed_until) > datetime.now(UTC)
    assert row.updated_at == before.updated_at
    assert (len(ledger.audit_trail()), len(ledger.events())) == (audit_before, events_before)

    assert ledger.claim_observation(REPOSITORY, PEER, 60) is False  # live: the peer waits its turn
    assert ledger.get_repository(REPOSITORY).claimed_by == REPLICA
    assert ledger.claim_observation(REPOSITORY, REPLICA, 60) is False  # the holder too — a rate, not a lock

    assert ledger.claim_observation("ghost/none", REPLICA, 60) is False  # nothing to observe

    # lapsed: the next replica through takes it — a zero-length lease lapses at once
    ledger.register_repository(RepositoryRecord(repository=NEIGHBOR, team="others"), actor="t")
    assert ledger.claim_observation(NEIGHBOR, REPLICA, 0) is True
    assert ledger.claim_observation(NEIGHBOR, PEER, 60) is True
    assert ledger.get_repository(NEIGHBOR).claimed_by == PEER
