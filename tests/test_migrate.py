"""Schema migrations: the discovery invariants, and the real thing on Postgres.

The offline half guards the migration *set* — names, order, gaps, drift — and
runs everywhere. The second half needs a database: it is skipped unless
``LEDGER_DSN`` points at one, and it is what CI's `migrate` job exercises (and
what gate A7 rehearses by hand). Each database-backed test gets its own
throwaway database, so the suite never touches the dev rig's own ledger.
"""

import os
from pathlib import Path
from dataclasses import fields
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import (
    MIGRATIONS_DIR,
    REGISTRY,
    Migration,
    MigrationError,
    discover,
    migrate,
    pending,
    status,
)
from control_plane.models import BurstRecord, TaskRecord, Violation
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo

# ---------------------------------------------------------------- offline


def _write(directory, *names):
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("SELECT 1;", encoding="utf-8")
    return directory


def test_shipped_migrations_are_a_valid_set():
    """The real directory: whatever it holds, it holds legally."""
    found = discover()
    assert [m.version for m in found] == [f"{i:04d}" for i in range(1, len(found) + 1)]
    assert found[0].name == "initial"
    assert "CREATE TABLE tasks" in found[0].sql


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_Caps.sql", "initial.sql"])
def test_filename_must_be_numbered(tmp_path, name):
    with pytest.raises(MigrationError, match="NNNN_slug.sql"):
        discover(_write(tmp_path / "m", name))


def test_duplicate_version_is_a_collision(tmp_path):
    """Two branches both claiming 0002 fail in CI, not in whatever order the
    filesystem happened to list them."""
    with pytest.raises(MigrationError, match="duplicate migration 0002"):
        discover(_write(tmp_path / "m", "0001_a.sql", "0002_b.sql", "0002_c.sql"))


def test_versions_must_be_contiguous(tmp_path):
    with pytest.raises(MigrationError, match="contiguous"):
        discover(_write(tmp_path / "m", "0001_a.sql", "0003_c.sql"))


def test_pending_skips_what_is_applied(tmp_path):
    available = discover(_write(tmp_path / "m", "0001_a.sql", "0002_b.sql", "0003_c.sql"))
    assert [m.version for m in pending(set(), available)] == ["0001", "0002", "0003"]
    assert [m.version for m in pending({"0001", "0002"}, available)] == ["0003"]
    assert pending({"0001", "0002", "0003"}, available) == []


def test_a_database_ahead_of_the_build_is_an_error(tmp_path):
    """A stale checkout pointed at a newer ledger must refuse, not re-apply."""
    available = discover(_write(tmp_path / "m", "0001_a.sql"))
    with pytest.raises(MigrationError, match="does not ship: 0002"):
        pending({"0001", "0002"}, available)


def test_migration_reads_its_sql_lazily(tmp_path):
    path = tmp_path / "0001_x.sql"
    path.write_text("CREATE TABLE x ();", encoding="utf-8")
    assert Migration(version="0001", name="x", path=path).sql == "CREATE TABLE x ();"
    assert str(Migration(version="0001", name="x", path=path)) == "0001_x"


def test_the_initial_migration_leaves_the_walls_to_a4():
    """A4 owns the walls; 0001 must not quietly pre-empt them (and if it ever
    does, the note in its header goes stale)."""
    sql = (MIGRATIONS_DIR / "0001_initial.sql").read_text(encoding="utf-8")
    statements = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "CHECK" not in statements
    assert "REFERENCES" not in statements


# ------------------------------------------------------- against Postgres

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
needs_postgres = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset — no Postgres to migrate")


@pytest.fixture
def fresh_dsn():
    """An empty database of its own, dropped afterwards."""
    name = f"migrate_test_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(DSN, dbname=name)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _columns(conn, table):
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
    ).fetchall()
    return {row[0] for row in rows}


@needs_postgres
def test_migrate_from_zero_then_idempotent(fresh_dsn):
    applied = migrate(fresh_dsn)
    assert [m.version for m in applied] == [m.version for m in discover()]
    assert migrate(fresh_dsn) == []  # the whole point: a second run writes nothing

    with psycopg.connect(fresh_dsn) as conn:
        recorded = conn.execute(f"SELECT version, applied_at FROM {REGISTRY} ORDER BY version").fetchall()
    assert [row[0] for row in recorded] == [m.version for m in applied]
    assert all(row[1] is not None for row in recorded)


@needs_postgres
def test_status_reads_without_writing(fresh_dsn):
    applied, todo = status(fresh_dsn)
    assert applied == []
    assert [m.version for m in todo] == [m.version for m in discover()]
    with psycopg.connect(fresh_dsn) as conn:
        assert conn.execute("SELECT to_regclass(%s)", (REGISTRY,)).fetchone()[0] is None

    migrate(fresh_dsn)
    applied, todo = status(fresh_dsn)
    assert applied == [m.version for m in discover()]
    assert todo == []


@needs_postgres
@pytest.mark.parametrize(
    ("table", "record", "extra"),
    [("tasks", TaskRecord, set()), ("bursts", BurstRecord, set()), ("violations", Violation, {"id"})],
)
def test_schema_speaks_the_ledger_vocabulary(fresh_dsn, table, record, extra):
    """The columns are exactly the dataclass fields — the drift guard A3's
    PostgresLedger will lean on, since it binds records by field name."""
    migrate(fresh_dsn)
    with psycopg.connect(fresh_dsn) as conn:
        assert _columns(conn, table) == {f.name for f in fields(record)} | extra


@needs_postgres
def test_a_migration_that_cannot_get_its_lock_fails_instead_of_queueing(
    fresh_dsn, tmp_path, monkeypatch
):
    """lock_timeout is the point: a DDL statement waiting on a lock blocks every
    query that arrives behind it, so failing fast is the kind thing to do."""
    monkeypatch.setattr("control_plane.migrate.LOCK_TIMEOUT", "200ms")
    with psycopg.connect(fresh_dsn, autocommit=True) as setup:
        setup.execute("CREATE TABLE probe (id integer)")
    (tmp_path / "0001_alter-probe.sql").write_text(
        "ALTER TABLE probe ADD COLUMN added text;", encoding="utf-8"
    )

    with psycopg.connect(fresh_dsn) as holder:
        holder.execute("LOCK TABLE probe IN ACCESS EXCLUSIVE MODE")  # held until commit
        with pytest.raises(psycopg.errors.LockNotAvailable):
            migrate(fresh_dsn, tmp_path)

    applied, todo = status(fresh_dsn, tmp_path)
    assert applied == []  # rolled back whole: the state after a failure is a prefix
    assert [str(m) for m in todo] == ["0001_alter-probe"]


@needs_postgres
def test_a_runaway_migration_is_cut_off(fresh_dsn, tmp_path, monkeypatch):
    monkeypatch.setattr("control_plane.migrate.STATEMENT_TIMEOUT", "200ms")
    (tmp_path / "0001_slow.sql").write_text("SELECT pg_sleep(5);", encoding="utf-8")

    with pytest.raises(psycopg.errors.QueryCanceled):
        migrate(fresh_dsn, tmp_path)
    assert status(fresh_dsn, tmp_path)[0] == []


@needs_postgres
def test_a_pre_a6_row_stops_the_migration_where_it_should(fresh_dsn):
    """0008 is *meant* to fail on MVP-era data, and this is the only place that
    claim gets tested — on an empty database it passes vacuously.

    A task row keyed by a bare issue number has no `issue_number` and cannot be
    given one by SQL: the id needs a repository slug, and the column it replaced
    held a filesystem path. So the migration stops, the state is a prefix (through
    0007, not 0008), and `control-plane backfill` is the documented repair.
    """
    available = discover()
    through_0007 = [m for m in available if m.version <= "0007"]

    # apply everything up to the rename, then put an MVP-era row in the way
    migrate(fresh_dsn, _slice(available, "0005"))
    with psycopg.connect(fresh_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, repo, status) VALUES ('7', '/Users/x/clones/product', 'done')"
        )

    with pytest.raises(psycopg.errors.CheckViolation, match="tasks_issue_number_not_null"):
        migrate(fresh_dsn)

    applied, pending_now = status(fresh_dsn)
    assert applied == [m.version for m in through_0007]
    assert [m.version for m in pending_now] == ["0008"]

    # the repair the header names, in miniature: give the row an identity
    with psycopg.connect(fresh_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE tasks SET task_id = 'owner/product#7', repository = 'owner/product', "
            "issue_number = 7 WHERE task_id = '7'"
        )
    assert [m.version for m in migrate(fresh_dsn)] == ["0008"]


def _slice(available, last_version):
    """A directory holding just the migrations up to `last_version`.

    The runner enforces contiguity from 0001, so a partial apply has to be a
    partial *directory* rather than a limit passed in.
    """
    import shutil
    import tempfile

    staging = Path(tempfile.mkdtemp())
    for migration in available:
        if migration.version <= last_version:
            shutil.copy(migration.path, staging / migration.path.name)
    return staging


@needs_postgres
def test_timestamps_are_timestamptz(fresh_dsn):
    """Real temporal types in the database, ISO strings at the adapter edge (A3)."""
    migrate(fresh_dsn)
    with psycopg.connect(fresh_dsn) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (column_name LIKE '%_at' OR column_name = 'at')"
        ).fetchall()
    assert rows, "no timestamp columns found — did the schema change shape?"
    assert {row[2] for row in rows} == {"timestamp with time zone"}
