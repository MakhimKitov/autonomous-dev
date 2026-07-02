"""Platform stores: session archive roundtrip + ledger roundtrip."""

import pytest
from control_plane.models import BurstRecord, BurstStatus, TaskRecord, Violation
from control_plane.store import LocalSessionArchive, SqliteLedger


def test_archive_roundtrip(tmp_path):
    src = tmp_path / "volume"
    (src / "projects" / "p").mkdir(parents=True)
    (src / "projects" / "p" / "sess.jsonl").write_text('{"turn":1}\n', encoding="utf-8")

    archive = LocalSessionArchive(tmp_path / "store")
    key, checksum = archive.put("42", src)
    assert key and len(checksum) == 64

    dest = tmp_path / "restored"
    assert archive.get("42", dest) is True
    assert (dest / "projects" / "p" / "sess.jsonl").read_text() == '{"turn":1}\n'


def test_credentials_never_enter_the_archive(tmp_path):
    """CLAUDE_CONFIG_DIR is the harvested volume — .credentials.json must not
    ride into the object store."""
    src = tmp_path / "volume"
    src.mkdir()
    (src / ".credentials.json").write_text('{"token":"secret"}', encoding="utf-8")
    (src / "ok.txt").write_text("keep", encoding="utf-8")

    archive = LocalSessionArchive(tmp_path / "store")
    archive.put("42", src)
    dest = tmp_path / "restored"
    archive.get("42", dest)
    assert (dest / "ok.txt").is_file()
    assert not (dest / ".credentials.json").exists()


def test_archive_missing_task(tmp_path):
    archive = LocalSessionArchive(tmp_path / "store")
    assert archive.get("nope", tmp_path / "dest") is False


def test_archive_checksum_mismatch_raises(tmp_path):
    src = tmp_path / "volume"
    src.mkdir()
    (src / "f").write_text("x", encoding="utf-8")
    archive = LocalSessionArchive(tmp_path / "store")
    archive.put("42", src)
    blob = tmp_path / "store" / "42" / "session.tar.gz"
    blob.write_bytes(blob.read_bytes() + b"tampered")
    with pytest.raises(RuntimeError, match="checksum"):
        archive.get("42", tmp_path / "dest")


def test_ledger_task_and_burst_roundtrip(tmp_path):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    assert ledger.get_task("42") is None

    ledger.upsert_task(TaskRecord(task_id="42", repo="/r"))
    task = ledger.get_task("42")
    assert task is not None and task.repo == "/r"

    task.session_id = "sess-1"
    ledger.upsert_task(task)
    assert ledger.get_task("42").session_id == "sess-1"

    burst = BurstRecord(burst_id="b1", task_id="42", trace_id="0" * 32, model="claude-sonnet-5")
    ledger.record_burst(burst)
    burst.status = BurstStatus.SUCCEEDED
    burst.exit_code = 0
    burst.cost_usd = 0.5
    burst.model = "claude-sonnet-5-20260203"  # harvest overwrote requested with resolved
    ledger.finish_burst(burst)
    row = ledger._conn.execute("SELECT model FROM bursts WHERE burst_id='b1'").fetchone()
    assert row["model"] == "claude-sonnet-5-20260203"

    ledger.record_violation(Violation(task_id="42", burst_id="b1", tool="Read", reason="WALL"))
    found = ledger.violations("42")
    assert len(found) == 1 and found[0].tool == "Read"
    assert ledger.violations("other") == []


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
