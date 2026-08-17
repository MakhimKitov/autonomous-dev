"""The walls, provoked (A4, #15).

STORES.md's "Walls in the schema" table names six guarantees the database is
supposed to enforce. This file attacks each of them and requires an error. That
is not belt-and-braces: two of the six — the `roles_pipeline_guard` trigger and
the append-only grants — are invisible to `atlas schema diff`, which reads
neither triggers nor privileges, so these tests are the *only* thing standing
behind them. A migration that dropped the trigger would pass CI's drift check.

The style throughout is: one statement, in its own transaction, expected to fail
with a specific error class. Raw SQL rather than the `Ledger` protocol, because a
wall's job is to stop writes the protocol would never make — the store layer is
careful, and the point of a wall is what happens when something isn't.

Two things worth knowing about the setup:

* `SET LOCAL ROLE platform_app` is how the grant walls are tested. It needs the
  migrating role to be a superuser or a member of platform_app, which is true of
  the dev rig and of CI (both make platform_admin the cluster's superuser) and is
  not assumed anywhere in the platform's own code.
* Postgres only. The single-node SQLite variant does not carry these walls, and
  deliberately: SQLite cannot add a constraint to an existing table, so a wall put
  in its bootstrap DDL would exist in ledgers created after the change and be
  missing from every older file, with nothing able to tell you which you hold.
  A6 (#17) retires that ledger as an origin. The races these walls catch need two
  writers, and the SQLite variant is one process by construction.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.models import BurstStatus, TaskStatus, Violation
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()

pytestmark = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset — no database to provoke")

PIPELINE_ROLES = ("dev", "reviewer", "tester", "monitor")

# Enough of a role row to insert one; the columns are all NOT NULL.
CUSTOM_ROLE = (
    "INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, "
    "max_turns, mount_mode, identity, sink) VALUES "
    "(%s, 'custom', '', '[]', 'claude-sonnet-5', 1.0, 10, 'ro', %s, %s)"
)


@pytest.fixture(scope="module")
def walls_dsn():
    """One migrated database for this module, dropped afterwards."""
    name = f"schema_walls_{uuid4().hex[:8]}"
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
def db(walls_dsn):
    """An autocommit connection over an empty ledger, seeded roles intact."""
    with psycopg.connect(walls_dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries")
        conn.execute("DELETE FROM roles WHERE kind = 'custom'")  # a previous test's leftovers
        yield conn


def attempt(conn, sql, params=None, *, role=None):
    """One statement, in its own transaction, optionally as another role.

    The transaction is what keeps a refused statement from poisoning the next
    one, and `SET LOCAL ROLE` reverts with it.
    """
    with conn.transaction():
        if role is not None:
            conn.execute(f"SET LOCAL ROLE {role}")
        return conn.execute(sql, params)


def a_task(conn, task_id="t1", status=TaskStatus.OPEN):
    attempt(conn, "INSERT INTO tasks (task_id, repo, status) VALUES (%s, '/r', %s)", (task_id, status))
    return task_id


def a_running_burst(conn, burst_id, task_id="t1", role="dev", kind="work"):
    attempt(
        conn,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status) VALUES (%s, %s, %s, %s, 'running')",
        (burst_id, task_id, kind, role),
    )
    return burst_id


def finish(conn, burst_id):
    attempt(
        conn,
        "UPDATE bursts SET status = 'succeeded', finished_at = now() WHERE burst_id = %s",
        (burst_id,),
    )


# ------------------------------------------------- one running burst per task


def test_only_one_burst_runs_per_task(db):
    """The wall that makes a doubled burst unrepresentable rather than unlikely."""
    a_task(db)
    a_running_burst(db, "b1")

    with pytest.raises(psycopg.errors.UniqueViolation, match="bursts_one_running_per_task"):
        a_running_burst(db, "b2")


def test_the_handoff_still_works(db):
    """The index is partial, so history piles up freely — it is only *running*
    rows that are unique. A task's next burst starts as soon as the last one has
    finished, which is the whole scheduler loop.

    (Observed while building this: a single data-modifying CTE that finalizes one
    burst and inserts the next trips the index unless the INSERT consumes the
    UPDATE's RETURNING. Postgres does not order unreferenced CTEs, so the two race
    against the same index. The store layer's two statements are unaffected, and
    anything combining them later must carry the data dependency.)"""
    a_task(db)
    a_running_burst(db, "b1")
    finish(db, "b1")
    a_running_burst(db, "b2")
    finish(db, "b2")
    a_running_burst(db, "b3")

    rows = db.execute("SELECT count(*) FROM bursts WHERE task_id = 't1'").fetchone()
    assert rows[0] == 3


# ------------------------------------------------------ finalize exactly once


@pytest.mark.parametrize(
    ("label", "sql"),
    [
        ("finished without a finish time", "UPDATE bursts SET status = 'succeeded' WHERE burst_id = 'b1'"),
        ("running with one", "UPDATE bursts SET finished_at = now() WHERE burst_id = 'b1'"),
    ],
)
def test_a_burst_is_never_half_finished(db, label, sql):
    """The CAS in the store layer makes the second finalize a no-op; this is what
    makes it structural. No statement can leave a row in a state that says both
    "still running" and "over" — the pair moves together or not at all."""
    a_task(db)
    a_running_burst(db, "b1")

    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_running_finished_check"):
        attempt(db, sql)


def test_a_finished_burst_cannot_be_reopened(db):
    a_task(db)
    a_running_burst(db, "b1")
    finish(db, "b1")

    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_running_finished_check"):
        attempt(db, "UPDATE bursts SET finished_at = NULL WHERE burst_id = 'b1'")


# --------------------------------------------- enumerations and the code's own


@pytest.mark.parametrize("status", list(TaskStatus))
def test_every_task_status_the_code_writes_is_allowed(db, status):
    """The CHECK and the StrEnum have to agree; a status the scheduler sets and
    the database rejects would be a wall in the wrong place."""
    a_task(db, task_id=f"t-{status}", status=status)


@pytest.mark.parametrize("status", list(BurstStatus))
def test_every_burst_status_the_code_writes_is_allowed(db, status):
    a_task(db)
    ended = None if status == BurstStatus.RUNNING else datetime(2026, 8, 14, tzinfo=UTC)
    attempt(
        db,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, finished_at) "
        "VALUES (%s, 't1', 'work', 'dev', %s, %s)",
        (f"b-{status}", status, ended),
    )


def test_a_status_nobody_implements_is_refused(db):
    with pytest.raises(psycopg.errors.CheckViolation, match="tasks_status_check"):
        a_task(db, status="vibing")

    # …and with a finish time, since a nonsense status with none trips the
    # running ⟺ finished_at CHECK first: the two interlock, and Postgres reports
    # whichever it reaches. This provokes the status CHECK alone.
    a_task(db)
    with pytest.raises(psycopg.errors.CheckViolation, match="bursts_status_check"):
        attempt(
            db,
            "INSERT INTO bursts (burst_id, task_id, kind, role, status, finished_at) "
            "VALUES ('b1', 't1', 'work', 'dev', 'vibing', now())",
        )


def test_burst_kind_is_deliberately_open(db):
    """`kind` is scheduler vocabulary — 'assess' arrived without a migration and
    the next one should too. The absence of a CHECK here is a decision."""
    a_task(db)
    a_running_burst(db, "b1", kind="archaeology")


def test_a_burst_points_at_a_real_task_and_a_real_role(db):
    a_task(db)

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_task_fk"):
        a_running_burst(db, "b1", task_id="no-such-task")

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_role_fk"):
        a_running_burst(db, "b2", role="ghost")


def test_a_burst_descends_from_a_burst_that_exists(db):
    """The session tree's edge is a foreign key (A5): a parent that isn't a burst
    is not a parent, so lineage can be walked without checking whether each hop
    is real."""
    a_task(db)

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_resumed_from_fk"):
        attempt(
            db,
            "INSERT INTO bursts (burst_id, task_id, kind, role, status, resumed_from_burst_id) "
            "VALUES ('b1', 't1', 'work', 'dev', 'running', 'no-such-burst')",
        )

    a_running_burst(db, "b1")
    finish(db, "b1")
    attempt(
        db,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, resumed_from_burst_id) "
        "VALUES ('b2', 't1', 'work', 'dev', 'running', 'b1')",
    )
    assert db.execute("SELECT resumed_from_burst_id FROM bursts WHERE burst_id='b2'").fetchone() == (
        "b1",
    )


def test_a_role_with_history_cannot_be_deleted(db):
    """Not the trigger this time — the plain foreign key. Even a custom role stops
    being disposable once a burst ran as it, because deleting it would leave
    history describing a role nobody can look up."""
    attempt(db, CUSTOM_ROLE, ("lens", "none", "issue_comment"))
    a_task(db)
    a_running_burst(db, "b1", role="lens")

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="bursts_role_fk"):
        attempt(db, "DELETE FROM roles WHERE role = 'lens'")


# ----------------------------------------------- the pipeline roles are frozen


def test_the_pipeline_roles_are_seeded(db):
    rows = db.execute(
        "SELECT role, kind, identity, sink, tools FROM roles WHERE kind = 'pipeline' ORDER BY role"
    ).fetchall()
    assert [row[0] for row in rows] == sorted(PIPELINE_ROLES)
    # identity 'none' for all four is the wall, not an omission: a pipeline role's
    # credential is engine code keyed on its name, and the column can only name
    # the shared automation App.
    assert {row[2] for row in rows} == {"none"}
    # tools '["*"]' — no narrowing, since no per-role allowlist exists in code yet
    assert all(row[4] == ["*"] for row in rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identity", "'automation'"),  # the funnel slot gets the wrong credential
        ("kind", "'custom'"),  # …or stops being a funnel slot at all
        ("sink", "'ledger_artifact'"),  # …or reports somewhere else
        ("tools", "'[\"Bash\"]'"),  # …or gains a tool the role never had
        ("role", "'dev2'"),  # renaming is the same escape: identity keys on the name
    ],
)
def test_a_pipeline_row_cannot_have_its_wall_fields_edited(db, field, value):
    with pytest.raises(psycopg.errors.RestrictViolation, match="frozen"):
        attempt(db, f"UPDATE roles SET {field} = {value} WHERE role = 'dev'")


def test_a_pipeline_row_cannot_be_deleted(db):
    """A row that can be dropped and re-inserted is not frozen."""
    with pytest.raises(psycopg.errors.RestrictViolation, match="cannot be deleted"):
        attempt(db, "DELETE FROM roles WHERE role = 'reviewer'")


def test_the_open_fields_stay_open(db):
    """The other half of roles-as-rows, and D5's (#31) whole premise: composition
    is data. Preamble, model, budget, turns and mount are editable on a pipeline
    row — only the wall-bearing fields are not."""
    attempt(
        db,
        "UPDATE roles SET prompt_preamble = 'Be terse.', model = 'claude-opus-5', "
        "max_budget_usd = 9.50, max_turns = 42, mount_mode = 'rw' WHERE role = 'reviewer'",
    )
    row = db.execute(
        "SELECT prompt_preamble, model, max_budget_usd, max_turns FROM roles WHERE role = 'reviewer'"
    ).fetchone()
    assert row == ("Be terse.", "claude-opus-5", 9.50, 42)


def test_a_custom_role_is_an_ordinary_row(db):
    """The trigger fires on `OLD.kind = 'pipeline'` and nothing else: custom roles
    — §10's review lenses among them — are created, edited and dropped freely."""
    attempt(db, CUSTOM_ROLE, ("lens", "none", "issue_comment"))
    attempt(db, "UPDATE roles SET identity = 'automation', tools = '[\"Read\"]' WHERE role = 'lens'")
    attempt(db, "DELETE FROM roles WHERE role = 'lens'")
    assert db.execute("SELECT count(*) FROM roles WHERE role = 'lens'").fetchone()[0] == 0


@pytest.mark.parametrize("identity", ["dev-app", "reviewer", "tester-app", ""])
def test_identity_never_names_a_pipeline_app(db, identity):
    """Ceilings in code: the column takes 'none' or the shared automation App.
    A pipeline App is not a value it accepts, from any row, pipeline or custom."""
    with pytest.raises(psycopg.errors.CheckViolation, match="roles_identity_check"):
        attempt(db, CUSTOM_ROLE, ("lens", identity, "issue_comment"))


def test_sinks_come_from_the_platform_implemented_set(db):
    with pytest.raises(psycopg.errors.CheckViolation, match="roles_sink_check"):
        attempt(db, CUSTOM_ROLE, ("lens", "none", "slack"))


# --------------------------------------------------------------- memory scope


@pytest.mark.parametrize(
    ("scope", "columns", "values", "constraint"),
    [
        ("product", "", "", "memory_entries_product_scope_check"),
        ("role", "", "", "memory_entries_role_scope_check"),
        ("platform", ", role", ", 'dev'", "memory_entries_role_scope_check"),
        ("role", ", repository", ", NULL", "memory_entries_role_scope_check"),
    ],
)
def test_a_memory_entry_names_what_its_scope_requires(db, scope, columns, values, constraint):
    """Both CHECKs are biconditionals, so they catch the pointer missing *and*
    the pointer nobody asked for — a 'platform' claim scoped to a role is as
    wrong as a 'role' claim scoped to nothing."""
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        attempt(
            db,
            f"INSERT INTO memory_entries (scope, claim, evidence, status{columns}) "
            f"VALUES (%s, 'c', 'e', 'proposed'{values})",
            (scope,),
        )


def test_a_well_scoped_memory_entry_is_accepted(db):
    attempt(
        db,
        "INSERT INTO memory_entries (scope, role, claim, evidence, status) "
        "VALUES ('role', 'dev', 'c', 'e', 'proposed')",
    )
    attempt(
        db,
        "INSERT INTO memory_entries (scope, claim, evidence, status) "
        "VALUES ('platform', 'c', 'e', 'proposed')",
    )


# ------------------------------------------------------- append-only by grant


@pytest.mark.parametrize("table", ["violations", "audit", "events"])
@pytest.mark.parametrize("verb", ["UPDATE {} SET at = now()", "DELETE FROM {}"])
def test_the_evidence_tables_are_append_only(db, table, verb):
    """A grant, not a promise. The runtime that records evidence cannot revise it,
    and the outbox it writes cannot be un-written. Invisible to the drift check:
    Atlas reads no privileges, so this test is the whole guard."""
    statement = verb.format(table)
    if table == "events" and "at = now()" in statement:
        statement = "UPDATE events SET created_at = now()"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, statement, role="platform_app")


@pytest.mark.parametrize("table", ["tasks", "bursts"])
def test_the_runtime_cannot_delete_its_own_state(db, table):
    """No DELETE grant anywhere: retention is platform_admin's, on a horizon."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, f"DELETE FROM {table}", role="platform_app")


@pytest.mark.parametrize(
    ("what", "statement"),
    [
        (
            "a trust slot",
            "INSERT INTO roles (role, kind, prompt_preamble, tools, model, max_budget_usd, "
            "max_turns, mount_mode, identity, sink) VALUES "
            "('shadow', 'pipeline', '', '[]', 'claude-sonnet-5', 1.0, 10, 'rw', 'none', 'event')",
        ),
        (
            "a product",
            "INSERT INTO repositories (repository, team, state, review_enabled, review_provider, "
            "e2e_enabled, test_env_refs, concurrency, daily_bursts, usage_share) "
            "VALUES ('o/r', 't', 'active', true, 'copilot', false, '{}', 1, 10, 1.0)",
        ),
        (
            "a subscription",
            "INSERT INTO subscriptions (subscription_id, member, secret_ref, cap_tokens, "
            "expires_at, status) VALUES ('s1', 'm', 'env:TOKEN', 100, now(), 'active')",
        ),
    ],
)
def test_the_runtime_cannot_register_what_operators_register(db, what, statement):
    """Registration, role composition and the subscription pool are operator
    verbs. Each grows the app role a grant in the stage that ships it (C1, D5,
    F1) — until then the runtime reads them and nothing more."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        attempt(db, statement, role="platform_app")


def test_the_runtime_can_do_its_job(db):
    """The other direction, and the one a too-tight grant would break: everything
    the scheduler actually does, done as the role replicas connect as."""
    attempt(db, "INSERT INTO tasks (task_id, repo, status) VALUES ('t1', '/r', 'open')", role="platform_app")
    attempt(db, "UPDATE tasks SET status = 'working' WHERE task_id = 't1'", role="platform_app")
    attempt(
        db,
        "INSERT INTO bursts (burst_id, task_id, kind, role, status) VALUES ('b1','t1','work','dev','running')",
        role="platform_app",
    )
    attempt(
        db,
        "UPDATE bursts SET status='succeeded', finished_at=now() WHERE burst_id='b1'",
        role="platform_app",
    )
    attempt(
        db,
        "INSERT INTO violations (task_id, burst_id, tool, reason) VALUES ('t1','b1','Bash','WALL')",
        role="platform_app",
    )
    attempt(db, "INSERT INTO audit (actor, action, target) VALUES ('op','stop','t1')", role="platform_app")
    for readable in ("roles", "repositories", "subscriptions", "memory_entries", "events"):
        attempt(db, f"SELECT count(*) FROM {readable}", role="platform_app")


def test_the_outbox_statement_runs_under_the_runtime_grants(db, walls_dsn):
    """`record_violation` is one data-modifying CTE writing two tables and
    returning through pg_notify. Grants are checked per table *and* per returned
    column, so this is the statement most likely to work as platform_admin in a
    test and fail as platform_app in production. Run the real method, as the real
    role — not a copy of its SQL."""
    from control_plane.postgres import PostgresLedger

    ledger = PostgresLedger(walls_dsn)
    try:
        ledger._conn.execute("SET ROLE platform_app")  # for the rest of the session
        db.execute("INSERT INTO tasks (task_id, repo, status) VALUES ('t1', '/r', 'open')")
        ledger.record_violation(Violation(task_id="t1", burst_id="b1", tool="Bash", reason="WALL"))

        assert [v.tool for v in ledger.violations("t1")] == ["Bash"]
        events = ledger.events()
        assert [e.kind for e in events] == ["violation_recorded"]
        assert events[0].payload["task_id"] == "t1"
    finally:
        ledger.close()


# ------------------------------------------------------------- the two-step


def test_no_constraint_is_left_unvalidated(db):
    """The guard on 0002's other half. A constraint added NOT VALID and never
    validated is enforced for new rows only, and every check we have is blind to
    it: squawk reads the SQL and sees a correct two-step, and `atlas schema diff`
    calls the database synced either way — measured, not assumed. This is what
    notices that 0003 (or its successor) was forgotten."""
    unvalidated = db.execute(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint "
        "WHERE NOT convalidated ORDER BY conname"
    ).fetchall()
    assert unvalidated == []
