"""What is specific to one backend, and nothing that isn't.

The behavior both backends of each store must share lives in the contract suites —
`test_ledger_contract.py` and `test_archive_contract.py` — each running one suite
against every implementation. What stays here is the single-node ledger's
bootstrap, which has no counterpart: Postgres gets its schema from numbered
migrations, and this one writes its own on open.
"""

import sqlite3

import pytest
from control_plane.models import BurstRecord, TaskRecord
from control_plane.store import LedgerShapeError, SqliteLedger

MVP_TASKS = (
    "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, repo TEXT NOT NULL, status TEXT NOT NULL, "
    "session_id TEXT, transcript_key TEXT, transcript_checksum TEXT, pr_number INTEGER, "
    "attempts INTEGER NOT NULL DEFAULT 0, review_rounds INTEGER NOT NULL DEFAULT 0, "
    "next_burst_kind TEXT, last_review_id INTEGER, last_burst_at TEXT, "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


def test_a_fresh_ledger_is_ready_on_open(tmp_path):
    """No runner, no migration: the file is created in the current shape."""
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    ledger.upsert_task(TaskRecord(task_id="owner/product#7", repository="owner/product", issue_number=7))
    ledger.record_burst(BurstRecord(burst_id="b1", task_id="owner/product#7"))

    loaded = ledger.get_task("owner/product#7")
    assert (loaded.repository, loaded.issue_number) == ("owner/product", 7)
    assert ledger.get_burst("b1").session_key is None  # nothing archived yet


def test_a_pre_a6_ledger_refuses_to_open_and_says_what_to_run(tmp_path):
    """A6 retires this ledger as an origin (STORES.md). Its rows are keyed by a
    bare issue number, which no longer identifies a task, and no `ALTER TABLE` can
    fix that — deriving an id needs a repository slug the file has never held. So
    the class refuses rather than opening something half-shaped, and names the
    one-shot that can move the rows."""
    db = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(MVP_TASKS)
    conn.execute(
        "INSERT INTO tasks (task_id, repo, status, created_at, updated_at) "
        "VALUES ('7', '/Users/x/clones/product', 'done', 'then', 'then')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerShapeError, match="control-plane backfill"):
        SqliteLedger(db)

    # and the file is untouched — the refusal is not a repair attempt
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT repo FROM tasks").fetchone()[0] == "/Users/x/clones/product"
    conn.close()
