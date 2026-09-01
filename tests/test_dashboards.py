"""The reference dashboards, held to the boundary they claim (B4, #22).

`examples/dashboards/` ships two copies of the same queries — one portable `.sql`
file and one Grafana dashboard that embeds it — because TO-BE §12 ships views and no
dashboard, so the example has to work in whatever the adopter already runs. Two
copies drift, and an example nobody runs rots quietly, so this file:

  * runs every statement against the real views, **as a role holding nothing but the
    two grants an operator is told to make** — which is the whole argument for
    shipping views instead of a dashboard, and the thing a panel silently breaks the
    day it reaches for a base table;
  * holds the JSON's copy of the SQL to the `.sql` file's, statement for statement;
  * pins the alert condition TO-BE §12 documents by firing it.

The static half needs no database and runs everywhere. The rest is Postgres-only,
like the views themselves.
"""

import json
import os
import pathlib
import re
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples" / "dashboards"
QUERIES = EXAMPLES / "reporting.sql"
DASHBOARD = EXAMPLES / "grafana-reporting.json"

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
requires_postgres = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset — no views to query")


def _statements() -> list[tuple[str, str]]:
    """The file's statements, each with the title of the comment that introduces it.

    Split on `;`, which is why no comment in that file may contain one.
    """
    named = []
    for chunk in QUERIES.read_text().split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        title = next(
            (m.group(1) for line in chunk.splitlines() if (m := re.match(r"--\s*\d+\.\s*([^.]+)", line))),
            "unnamed",
        )
        named.append((title.strip(), chunk))
    return named


STATEMENTS = _statements()
PANELS = json.loads(DASHBOARD.read_text())["panels"]


def _sql_only(statement: str) -> str:
    return "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--"))


def _flat(sql: str) -> str:
    return " ".join(sql.split())


# ------------------------------------------------------------- the two copies agree


def test_the_dashboard_is_the_sql_file_in_order():
    """One source, two renderings. Asserted in order because reporting.sql says its
    statements are "in the order the dashboard shows them" — a claim a reader uses to
    find the query behind a panel."""
    embedded = [_flat(p["targets"][0]["rawSql"]) for p in PANELS]
    assert embedded == [_flat(_sql_only(sql)) for _, sql in STATEMENTS]


def test_every_panel_reads_the_ledger_datasource():
    """The uid is the contract between the JSON and
    docker/telemetry/grafana/datasources.yaml, which is what binds the dashboard to
    the least-privileged role rather than to whatever is default."""
    for panel in PANELS:
        assert panel["datasource"]["uid"] == "ledger", panel["title"]
        assert panel["targets"][0]["datasource"]["uid"] == "ledger", panel["title"]
        assert panel["targets"][0]["rawQuery"] is True, panel["title"]


def test_no_panel_uses_a_tool_macro():
    """`$__timeFilter(...)` and friends would make these queries run in exactly one
    program. Windows are plain `now() - interval`, which every driver understands and
    a human can paste into psql."""
    for title, sql in STATEMENTS:
        assert "$__" not in _sql_only(sql), title


def test_the_time_series_panel_has_the_shape_every_tool_expects():
    """(time, metric, value) is the classic SQL time-series contract — the one shape a
    dashboard recognizes with no per-tool hint, and the reason that panel's columns
    are aliased to words that read oddly in psql."""
    title, sql = next((t, s) for t, s in STATEMENTS if "Spend" in t)
    assert re.findall(r"AS (\w+)", _sql_only(sql)) == ["time", "metric", "value"], title


def test_nothing_reads_a_base_table():
    """Static half of the boundary below: every table reference is a `reporting.`
    view. Cheap, runs with no database, and names the offending panel."""
    for title, sql in STATEMENTS:
        referenced = re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", _sql_only(sql), re.IGNORECASE)
        assert referenced, title
        for name in referenced:
            assert name.startswith("reporting."), f"{title}: {name}"


# ------------------------------------------------------------------- against the db


@pytest.fixture(scope="module")
def views_dsn():
    """One migrated database for this module, dropped afterwards."""
    name = f"dashboards_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def reader(views_dsn):
    """A role with exactly what docker/telemetry/dashboard-role.sql grants — no
    login, since `SET LOCAL ROLE` asks the same privilege questions and a test needs
    no password."""
    name = f"dashboard_{uuid4().hex[:8]}"
    with psycopg.connect(views_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {name} NOLOGIN")
        conn.execute(f"GRANT USAGE ON SCHEMA reporting TO {name}")
        conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO {name}")
    try:
        yield name
    finally:
        with psycopg.connect(views_dsn, autocommit=True) as conn:
            conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA reporting FROM {name}")
            conn.execute(f"REVOKE ALL ON SCHEMA reporting FROM {name}")
            conn.execute(f"DROP ROLE {name}")


@pytest.fixture
def as_reader(views_dsn, reader):
    """Run one statement the way the dashboard runs it, and return its rows."""

    def run(sql, params=None):
        with psycopg.connect(views_dsn, autocommit=True, row_factory=dict_row) as conn:
            with conn.transaction():
                conn.execute(f"SET LOCAL ROLE {reader}")
                return conn.execute(sql, params).fetchall()

    return run


@pytest.fixture
def db(views_dsn):
    with psycopg.connect(views_dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries, repositories")
        # the registry wall (C1): the suite's tasks have to name a registered product
        conn.execute(
            "INSERT INTO repositories (repository, team, state, review_enabled, review_provider, "
            "e2e_enabled, test_env_refs, concurrency, daily_bursts, usage_share) "
            "VALUES ('owner/product', 'owner', 'active', false, '', false, '{}', 1, 20, 1.0)"
        )
        yield conn


@requires_postgres
@pytest.mark.parametrize("title,sql", STATEMENTS, ids=[t for t, _ in STATEMENTS])
def test_every_reference_panel_runs_as_the_dashboard_role(as_reader, title, sql):
    """An example that has never been run is a guess. Against an empty ledger, so
    what is asserted is that the SQL is valid and the grants suffice — the numbers
    themselves are tests/test_reporting_views.py's subject."""
    as_reader(sql)


@requires_postgres
def test_the_role_this_suite_uses_really_is_walled(as_reader):
    """Negative control for the test above: it would prove nothing if the fixture
    happened to be privileged. `tests/test_reporting_views.py` owns the boundary
    itself — this only certifies that the panels were run from behind it."""
    for table in ("tasks", "bursts", "violations", "events"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_reader(f"SELECT * FROM {table}")


ALERT = next(sql for title, sql in STATEMENTS if "alert" in title.lower())


@requires_postgres
def test_the_alert_condition_is_quiet_until_a_wall_fires(db, as_reader):
    """TO-BE §12: any new violations row is a page. Rows out of this query *are* the
    page, so silence on a clean ledger is half the condition and has to be asserted
    as hard as the firing does."""
    db.execute(
        "INSERT INTO tasks (task_id, repository, issue_number, status) "
        "VALUES ('owner/product#1', 'owner/product', 1, 'working')"
    )
    db.execute(
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, finished_at) "
        "VALUES ('b1', 'owner/product#1', 'work', 'dev', 'succeeded', now())"
    )
    assert as_reader(ALERT) == []

    db.execute(
        "INSERT INTO violations (task_id, burst_id, tool, reason) "
        "VALUES ('owner/product#1', 'b1', 'Read', %s)",
        ("'private/' is off-limits: private/notes.md",),
    )
    paged = as_reader(ALERT)
    assert len(paged) == 1
    assert paged[0]["tool"] == "Read"
    assert paged[0]["reason"].startswith("'private/' is off-limits")
    assert paged[0]["hits"] == 1


@requires_postgres
def test_the_alert_stops_firing_once_the_hit_is_old(db, as_reader):
    """The window is what makes the pull form a *condition* rather than a total: an
    hour-old wall hit is history, and history belongs in the containment panel next
    to it. It is also why the window must exceed the poll interval — a hit that lands
    between two polls is never seen."""
    db.execute(
        "INSERT INTO tasks (task_id, repository, issue_number, status) "
        "VALUES ('owner/product#2', 'owner/product', 2, 'working')"
    )
    db.execute(
        "INSERT INTO violations (task_id, burst_id, tool, reason, at) "
        "VALUES ('owner/product#2', 'b9', 'Bash', 'sandbox-weakening flag', now() - interval '1 hour')"
    )
    assert as_reader(ALERT) == []
    still_there = as_reader("SELECT hits FROM reporting.containment")
    assert [row["hits"] for row in still_there] == [1]
