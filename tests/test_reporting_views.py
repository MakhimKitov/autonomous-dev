"""The reporting views, asked questions with known answers (B3, #21).

The views are the only place a metric is *defined* — "success rate" is a choice
about denominators, and this file is where that choice is pinned. Seeded rows in,
known numbers out; a view whose SQL is merely read looks correct in every case
that hasn't happened yet.

They are also `schema.sql`'s newest blind spot, and the loudest one: Atlas's
community version refuses a file containing `CREATE VIEW` outright, so the drift
check cannot see these definitions at all. That makes this file the only thing
standing behind them — the same position `test_schema_walls.py` holds for the
trigger and the grants.

Postgres only: views over a Postgres schema, on a Postgres feature (`FILTER`,
`security_invoker` defaulting off). The single-node ledger has no reporting layer
and is not supposed to grow one — a laptop reads its own numbers with a query.
"""

import os
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()

pytestmark = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset — no views to query")

PRODUCT = "owner/product"
OTHER = "owner/other"


@pytest.fixture(scope="module")
def views_dsn():
    """One migrated database for this module, dropped afterwards."""
    name = f"reporting_views_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def db(views_dsn):
    with psycopg.connect(views_dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(
            "TRUNCATE tasks, bursts, violations, events, memory_entries, repositories, subscriptions"
        )
        # the registry wall (C1): the suite's tasks have to name registered products
        conn.execute(
            "INSERT INTO repositories (repository, team, state, review_enabled, review_provider, "
            "e2e_enabled, test_env_refs, concurrency, daily_bursts, usage_share) VALUES "
            f"('{PRODUCT}', 'owner', 'active', false, '', false, '{{}}', 1, 20, 1.0), "
            f"('{OTHER}', 'owner', 'active', false, '', false, '{{}}', 1, 20, 1.0)"
        )
        yield conn


def task(db, issue_number, status, *, repository=PRODUCT, attempts=0, review_rounds=0):
    db.execute(
        "INSERT INTO tasks (task_id, repository, issue_number, status, attempts, review_rounds) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (f"{repository}#{issue_number}", repository, issue_number, status, attempts, review_rounds),
    )
    return f"{repository}#{issue_number}"


def burst(db, burst_id, task_id, *, kind="work", role="dev", status="succeeded", cost=None, turns=None,
          subscription=None, tokens=None, started=None):
    db.execute(
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, finished_at, cost_usd, num_turns, "
        "                    subscription_id, input_tokens, output_tokens, started_at) "
        "VALUES (%s, %s, %s, %s, %s, CASE WHEN %s = 'running' THEN NULL ELSE now() END, %s, %s, "
        "        %s, %s, %s, coalesce(%s, now()))",
        (burst_id, task_id, kind, role, status, status, cost, turns,
         subscription, tokens, tokens, started),
    )
    return burst_id


def subscription(db, member, *, cap=1_000_000, status="active", expires="2027-09-01"):
    db.execute(
        "INSERT INTO subscriptions (subscription_id, member, secret_ref, cap_tokens, expires_at, status) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (member, member, f"env:{member.upper()}_TOKEN", cap, expires, status),
    )
    return member


def violation(db, task_id, burst_id, *, tool="Read", reason="path escapes the repository clone: /etc/os-release"):
    db.execute(
        "INSERT INTO violations (task_id, burst_id, tool, reason, at) VALUES (%s, %s, %s, %s, now())",
        (task_id, burst_id, tool, reason),
    )


def one(db, sql, params=None):
    return db.execute(sql, params).fetchone()


# ------------------------------------------------------------------- task facts


def test_a_task_with_no_bursts_has_no_bursts(db):
    """0 and not NULL for counts, because a task with no bursts ran none — but
    money stays NULL, since 0.00 would claim a burst ran for free."""
    task(db, 1, "open")
    row = one(db, "SELECT * FROM reporting.task_facts WHERE issue_number = 1")
    assert (row["bursts"], row["violations"], row["bursts_with_violations"]) == (0, 0, 0)
    assert row["cost_usd"] is None
    assert row["first_burst_at"] is None


def test_the_per_task_numbers_are_the_ones_the_families_aggregate(db):
    tid = task(db, 2, "awaiting_review", attempts=2, review_rounds=1)
    burst(db, "b1", tid, kind="work", cost=0.50, turns=7)
    burst(db, "b2", tid, kind="review", role="reviewer", cost=0.10, turns=3)
    burst(db, "b3", tid, kind="test", role="tester", status="failed", cost=0.20, turns=1)
    violation(db, tid, "b1")
    violation(db, tid, "b1", tool="Bash", reason="command touches protected content ('.env')")

    row = one(db, "SELECT * FROM reporting.task_facts WHERE issue_number = 2")
    assert (row["bursts"], row["work_bursts"], row["review_bursts"], row["test_bursts"]) == (3, 1, 1, 1)
    assert row["bursts_that_did_not_finish_clean"] == 1
    assert float(row["cost_usd"]) == pytest.approx(0.80)
    assert row["turns"] == 11
    # two violations, one burst — the pair is what makes "per burst" answerable
    assert (row["violations"], row["bursts_with_violations"]) == (2, 1)


def test_a_killed_burst_did_not_finish_clean_either(db):
    """The budget rail killing a burst is a different event from a burst erroring,
    and neither one is a clean finish — so the column counts both."""
    tid = task(db, 3, "working")
    burst(db, "k1", tid, status="killed")
    burst(db, "k2", tid, status="failed")
    burst(db, "k3", tid, status="succeeded")
    row = one(db, "SELECT * FROM reporting.task_facts WHERE issue_number = 3")
    assert (row["bursts"], row["bursts_that_did_not_finish_clean"]) == (3, 2)


# ------------------------------------------------------------------ reliability


def test_rates_are_over_settled_tasks(db):
    """A task the scheduler is still working is not a failure yet. If in-flight
    tasks counted in the denominator, every rate would move with queue depth
    instead of with the platform's behavior."""
    task(db, 10, "done")
    task(db, 11, "done")
    task(db, 12, "blocked")
    task(db, 13, "working")  # in flight — in `tasks`, out of the denominator
    task(db, 14, "awaiting_review")

    row = one(db, "SELECT * FROM reporting.reliability WHERE repository = %s", (PRODUCT,))
    assert (row["tasks"], row["settled"], row["done"], row["blocked"]) == (5, 3, 2, 1)
    assert float(row["success_rate"]) == pytest.approx(2 / 3)
    assert float(row["blocked_rate"]) == pytest.approx(1 / 3)
    # needs-review is over the whole board on purpose: "a human has the ball" is a
    # fact about now, and its share of everything is the question being asked
    assert float(row["needs_review_rate"]) == pytest.approx(1 / 5)


def test_an_empty_denominator_is_null_not_an_error(db):
    """Nothing settled yet: a rate over zero tasks has no value, and NULL is what
    a dashboard renders as a gap rather than as 0%."""
    task(db, 20, "working")
    row = one(db, "SELECT * FROM reporting.reliability WHERE repository = %s", (PRODUCT,))
    assert row["settled"] == 0
    assert row["success_rate"] is None and row["blocked_rate"] is None


def test_rework_is_a_second_attempt_or_any_review_round(db):
    task(db, 30, "done")  # first pass
    task(db, 31, "done", attempts=2)  # retried
    task(db, 32, "done", review_rounds=1)  # sent back by a review
    row = one(db, "SELECT * FROM reporting.reliability WHERE repository = %s", (PRODUCT,))
    assert float(row["rework_rate"]) == pytest.approx(2 / 3)


def test_cost_is_per_task_and_per_repository(db):
    tid = task(db, 40, "done")
    burst(db, "c1", tid, cost=1.25)
    burst(db, "c2", tid, cost=0.75)
    other = task(db, 41, "done", repository=OTHER)
    burst(db, "c3", other, cost=3.00)

    rows = {
        row["repository"]: row
        for row in db.execute("SELECT * FROM reporting.reliability").fetchall()
    }
    assert float(rows[PRODUCT]["cost_usd"]) == pytest.approx(2.00)
    assert float(rows[OTHER]["cost_usd"]) == pytest.approx(3.00)
    assert float(rows[PRODUCT]["bursts_per_task"]) == pytest.approx(2.0)


def test_repositories_never_pool(db):
    """The tenancy unit is the product (TO-BE §2). Two products' numbers sharing a
    row would be the one mistake that makes every metric useless at once."""
    task(db, 50, "done")
    task(db, 51, "blocked", repository=OTHER)
    rows = db.execute("SELECT repository, done, blocked FROM reporting.reliability ORDER BY repository").fetchall()
    assert [(r["repository"], r["done"], r["blocked"]) for r in rows] == [
        (OTHER, 0, 1),
        (PRODUCT, 1, 0),
    ]


# ------------------------------------------------------------------ containment


def test_every_wall_hit_is_grouped_by_what_it_stopped(db):
    escape = "path escapes the repository clone: /etc/os-release"
    tid = task(db, 60, "done")
    burst(db, "v1", tid)
    burst(db, "v2", tid)
    violation(db, tid, "v1", tool="Read", reason=escape)
    violation(db, tid, "v2", tool="Read", reason=escape)
    violation(db, tid, "v2", tool="Bash", reason="sandbox-weakening flag ('--yolo')")

    rows = db.execute(
        "SELECT * FROM reporting.containment WHERE repository = %s ORDER BY hits DESC", (PRODUCT,)
    ).fetchall()
    assert [(r["tool"], r["hits"], r["bursts"], r["tasks"]) for r in rows] == [
        ("Read", 2, 2, 1),
        ("Bash", 1, 1, 1),
    ]
    assert rows[0]["reason"] == escape  # the wall's own sentence, ungrouped and unclassified
    assert rows[0]["first_seen"] is not None and rows[0]["last_seen"] is not None


def test_a_clean_repository_has_no_containment_rows(db):
    """Absence, not a zero row: `violations` is the experiment's primary dataset,
    and TO-BE §11 makes any new row a page — so "no rows" is the good state and the
    view says nothing rather than inventing a reassuring one."""
    task(db, 70, "done")
    assert db.execute("SELECT * FROM reporting.containment").fetchall() == []


def test_a_violation_outlives_the_burst_it_names(db):
    """`violations` has no foreign keys by design — evidence outlives its subject.
    The view must not quietly drop a violation whose burst row is gone, which an
    inner join to `bursts` would do."""
    tid = task(db, 80, "done")
    violation(db, tid, "a-burst-that-was-never-recorded")
    row = one(db, "SELECT * FROM reporting.containment WHERE repository = %s", (PRODUCT,))
    assert row["hits"] == 1


# ------------------------------------------------------------------- capability


def test_first_pass_merges_are_the_proxy_and_are_named_as_one(db):
    """A review verdict is a GitHub event; the ledger keeps the round count. So the
    view reports merges that needed no review round — a proxy for automated-review
    pass-rate, and named for what it actually counts."""
    task(db, 90, "done")  # merged, no rounds
    task(db, 91, "done", review_rounds=2)
    task(db, 92, "blocked", review_rounds=3)  # never merged: out of the denominator
    row = one(db, "SELECT * FROM reporting.capability WHERE repository = %s", (PRODUCT,))
    assert (row["merged"], row["merged_first_pass"]) == (2, 1)
    assert float(row["first_pass_rate"]) == pytest.approx(0.5)
    assert float(row["review_rounds_per_merge"]) == pytest.approx(1.0)


def test_test_bursts_are_counted_not_judged(db):
    """A tester burst succeeds while reporting defects, so its exit code is not a
    verdict. The view counts runs and non-clean finishes; what the report *said*
    becomes checkable at B6 (#68)."""
    tid = task(db, 100, "done")
    burst(db, "t1", tid, kind="test", role="tester", status="succeeded")
    burst(db, "t2", tid, kind="test", role="tester", status="failed")
    row = one(db, "SELECT * FROM reporting.capability WHERE repository = %s", (PRODUCT,))
    assert row["test_bursts"] == 2
    assert row["bursts_that_did_not_finish_clean"] == 1
    # the count of runs is the only test-shaped column: nothing here is a verdict
    assert [key for key in row if "test" in key] == ["test_bursts"]


# ------------------------------------------------- the views as a read boundary


def test_a_dashboard_role_reads_aggregates_and_not_rows(db, views_dsn):
    """The property that makes shipping views better than shipping a dashboard: a
    view runs with its owner's privileges (`security_invoker` is off by default), so
    a role with USAGE on `reporting` reads every metric and cannot read one row of
    `tasks`, `bursts` or `violations`. Provoked rather than assumed — this is the
    statement STORES.md tells an operator to run.
    """
    tid = task(db, 110, "done")
    burst(db, "d1", tid, cost=0.42)
    role = f"dashboard_{uuid4().hex[:8]}"
    db.execute(f"CREATE ROLE {role} NOLOGIN")
    db.execute(f"GRANT USAGE ON SCHEMA reporting TO {role}")
    db.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO {role}")
    try:
        with psycopg.connect(views_dsn, autocommit=True, row_factory=dict_row) as conn:
            with conn.transaction():
                conn.execute(f"SET LOCAL ROLE {role}")
                seen = conn.execute("SELECT done FROM reporting.reliability").fetchone()
                assert seen["done"] == 1
            for table in ("tasks", "bursts", "violations"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction():
                        conn.execute(f"SET LOCAL ROLE {role}")
                        conn.execute(f"SELECT * FROM {table}")
    finally:
        db.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA reporting FROM {role}")
        db.execute(f"REVOKE ALL ON SCHEMA reporting FROM {role}")
        db.execute(f"DROP ROLE {role}")


# ------------------------------------------------------------------ contribution


def test_a_contribution_that_has_not_been_spent_is_still_a_contribution(db):
    """The member opted in this morning: one row, `week` NULL and zeros — an
    absent row would read as "not contributing"."""
    subscription(db, "ada")
    row = one(db, "SELECT * FROM reporting.contribution WHERE subscription_id = 'ada'")
    assert (row["week"], row["bursts"], row["tokens"], row["products"]) == (None, 0, 0, 0)
    assert (row["status"], row["cap_tokens"], row["last_burst_at"]) == ("active", 1_000_000, None)


def test_a_week_of_contribution_is_bursts_tokens_and_products(db):
    subscription(db, "ada")
    subscription(db, "bo")
    first = task(db, 200, "done")
    other = task(db, 201, "done", repository=OTHER)
    burst(db, "c1", first, subscription="ada", tokens=100)
    burst(db, "c2", other, subscription="ada", tokens=250, status="failed")
    burst(db, "c3", first, subscription="bo", tokens=10)
    burst(db, "c4", first, tokens=9_999)  # the machine's own credential: nobody's contribution

    ada = one(db, "SELECT * FROM reporting.contribution WHERE subscription_id = 'ada'")
    assert (ada["bursts"], ada["bursts_succeeded"], ada["bursts_that_did_not_finish_clean"]) == (2, 1, 1)
    assert ada["tokens"] == 700  # input + output, both sides of each burst
    assert ada["products"] == 2 and ada["last_burst_at"] is not None
    bo = one(db, "SELECT * FROM reporting.contribution WHERE subscription_id = 'bo'")
    assert (bo["bursts"], bo["tokens"], bo["products"]) == (1, 20, 1)
    # the unattributed burst belongs to no row at all
    assert one(db, "SELECT count(*) AS n FROM reporting.contribution")["n"] == 2


def test_contribution_splits_by_week(db):
    subscription(db, "ada")
    first = task(db, 210, "done")
    burst(db, "w1", first, subscription="ada", tokens=100, started="2026-09-07T10:00:00+00:00")
    burst(db, "w2", first, subscription="ada", tokens=50, started="2026-09-15T10:00:00+00:00")
    rows = db.execute(
        "SELECT week, bursts, tokens FROM reporting.contribution "
        "WHERE subscription_id = 'ada' ORDER BY week"
    ).fetchall()
    assert [(row["bursts"], row["tokens"]) for row in rows] == [(1, 200), (1, 100)]
    assert rows[0]["week"].isoformat().startswith("2026-09-07")  # Monday of that week


def test_the_runtime_can_read_its_own_numbers(db):
    """`platform_app` gets USAGE and SELECT in 0010 — it may already read the base
    tables, so this grants nothing new; it just means the runtime doesn't have to
    re-derive a metric the schema already defines."""
    task(db, 120, "done")
    with db.transaction():
        db.execute("SET LOCAL ROLE platform_app")
        assert one(db, "SELECT count(*) AS n FROM reporting.task_facts")["n"] == 1


def test_no_view_can_be_written_through(db):
    """These are aggregates, so Postgres refuses on its own — but the refusal is
    what makes them safe to hand out, so it gets a test rather than an assumption."""
    task(db, 130, "done")
    for view in ("reporting.reliability", "reporting.containment", "reporting.capability",
                 "reporting.contribution"):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            with db.transaction():
                db.execute(f"DELETE FROM {view}")
