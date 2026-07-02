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

    burst = BurstRecord(burst_id="b1", task_id="42", trace_id="0" * 32)
    ledger.record_burst(burst)
    burst.status = BurstStatus.SUCCEEDED
    burst.exit_code = 0
    burst.cost_usd = 0.5
    ledger.finish_burst(burst)

    ledger.record_violation(Violation(task_id="42", burst_id="b1", tool="Read", reason="WALL"))
    found = ledger.violations("42")
    assert len(found) == 1 and found[0].tool == "Read"
    assert ledger.violations("other") == []
