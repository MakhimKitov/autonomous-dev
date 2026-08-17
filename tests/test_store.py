"""What is specific to one backend, and nothing that isn't.

The behavior both backends of each store must share lives in the contract suites —
`test_ledger_contract.py` and `test_archive_contract.py` — each running one suite
against every implementation. What stays here is SQLite's self-bootstrapping
schema, which has no counterpart: Postgres gets its schema from numbered
migrations, and this ledger writes its own on open.
"""

from control_plane.models import BurstRecord
from control_plane.store import SqliteLedger


def test_ledger_migrates_pre_model_schema(tmp_path):
    """A ledger created before the model column existed gains it on open."""
    import sqlite3

    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE bursts (burst_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, "
        "trace_id TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT, "
        "exit_code INTEGER, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, "
        "num_turns INTEGER, session_id TEXT)"
    )
    conn.execute(
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, started_at) "
        "VALUES ('old', '42', 'work', 'dev', 'succeeded', 'then')"
    )
    conn.commit()
    conn.close()

    ledger = SqliteLedger(db)  # opening migrates
    ledger.record_burst(BurstRecord(burst_id="new", task_id="42", model="claude-sonnet-5"))
    rows = {
        r["burst_id"]: r["model"]
        for r in ledger._conn.execute("SELECT burst_id, model FROM bursts")
    }
    assert rows == {"old": None, "new": "claude-sonnet-5"}


def test_ledger_migrates_pre_session_tree_schema(tmp_path):
    """And a ledger written before the session tree gains its three columns — the
    same additive path, and the reason the SQLite variant needs no migration
    runner. A row from before has no lineage, which is the truth about it."""
    import sqlite3

    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE bursts (burst_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL, "
        "trace_id TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT, "
        "exit_code INTEGER, model TEXT, cost_usd REAL, input_tokens INTEGER, "
        "output_tokens INTEGER, num_turns INTEGER, session_id TEXT)"
    )
    conn.execute(
        "INSERT INTO bursts (burst_id, task_id, kind, role, status, started_at) "
        "VALUES ('old', '42', 'work', 'dev', 'succeeded', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    ledger = SqliteLedger(db)
    assert ledger.get_burst("old").session_key is None
    assert ledger.latest_archived_burst("42") is None  # nothing archived, no leaf

    ledger.record_burst(BurstRecord(burst_id="new", task_id="42", resumed_from_burst_id="old"))
    assert ledger.get_burst("new").resumed_from_burst_id == "old"
