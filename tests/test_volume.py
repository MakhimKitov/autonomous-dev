"""Volume choreography: prepare (materialize) and harvest, full cycle."""

import json

from control_plane.models import BurstRecord, BurstStatus
from control_plane.store import LocalSessionArchive, SqliteLedger
from control_plane.volume import harvest_volume, prepare_volume
from dev_agent.env import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME


def test_prepare_fresh_volume_is_empty(tmp_path):
    archive = LocalSessionArchive(tmp_path / "store")
    volume = prepare_volume(tmp_path / "volumes", "b1", "42", archive)
    assert volume.is_dir() and not any(volume.iterdir())


def test_full_cycle_burst_then_resume(tmp_path):
    """Burst 1 writes state → harvest → burst 2's volume sees it again."""
    archive = LocalSessionArchive(tmp_path / "store")
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")

    volume1 = prepare_volume(tmp_path / "volumes", "b1", "42", archive)
    (volume1 / "projects").mkdir()
    (volume1 / "projects" / "sess.jsonl").write_text('{"turn":1}\n', encoding="utf-8")
    (volume1 / BURST_RESULT_FILENAME).write_text(
        json.dumps(
            {
                "session_id": "sess-1",
                "model": "claude-sonnet-5-20260203",
                "total_cost_usd": 0.42,
                "num_turns": 7,
                "is_error": False,
            }
        ),
        encoding="utf-8",
    )
    (volume1 / VIOLATIONS_FILENAME).write_text(
        json.dumps({"tool": "Read", "reason": "WALL: private/", "input": "{}"}) + "\n",
        encoding="utf-8",
    )

    burst = BurstRecord(burst_id="b1", task_id="42", model="claude-sonnet-5")
    outcome = harvest_volume(volume1, burst, archive, ledger)

    assert burst.session_id == "sess-1"
    assert burst.model == "claude-sonnet-5-20260203"  # resolved beats requested
    assert burst.cost_usd == 0.42
    assert burst.num_turns == 7
    assert outcome.violation_count == 1
    assert outcome.transcript_key
    assert len(ledger.violations("42")) == 1

    volume2 = prepare_volume(tmp_path / "volumes", "b2", "42", archive)
    assert (volume2 / "projects" / "sess.jsonl").read_text() == '{"turn":1}\n'


def test_harvest_marks_error_bursts_failed(tmp_path):
    archive = LocalSessionArchive(tmp_path / "store")
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    volume = prepare_volume(tmp_path / "volumes", "b1", "42", archive)
    (volume / BURST_RESULT_FILENAME).write_text(json.dumps({"is_error": True}), encoding="utf-8")

    burst = BurstRecord(burst_id="b1", task_id="42")
    harvest_volume(volume, burst, archive, ledger)
    assert burst.status == BurstStatus.FAILED


def test_reviewer_harvest_never_overwrites_the_dev_session(tmp_path):
    """The archive is task-keyed and holds the dev's resumable session; an
    assess burst harvesting under the same task id must leave it alone."""
    archive = LocalSessionArchive(tmp_path / "store")
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")

    dev_volume = prepare_volume(tmp_path / "volumes", "b1", "5", archive)
    (dev_volume / "projects").mkdir()
    (dev_volume / "projects" / "dev.jsonl").write_text('{"dev":1}\n', encoding="utf-8")
    harvest_volume(dev_volume, BurstRecord(burst_id="b1", task_id="5"), archive, ledger)

    reviewer_volume = prepare_volume(tmp_path / "volumes", "b2", "5", archive)
    (reviewer_volume / "projects" / "dev.jsonl").unlink()  # the reviewer starts fresh...
    (reviewer_volume / "projects" / "reviewer.jsonl").write_text('{"rev":1}\n', encoding="utf-8")
    outcome = harvest_volume(
        reviewer_volume, BurstRecord(burst_id="b2", task_id="5"), archive, ledger,
        archive_session=False,
    )
    assert outcome.transcript_key is None

    next_dev_volume = prepare_volume(tmp_path / "volumes", "b3", "5", archive)
    assert (next_dev_volume / "projects" / "dev.jsonl").exists()  # dev session intact
    assert not (next_dev_volume / "projects" / "reviewer.jsonl").exists()


def test_unseeded_volume_stays_empty_despite_archived_session(tmp_path):
    """seed=False (amnesiac reviewer): the dev's transcripts must not even
    materialize into the volume."""
    archive = LocalSessionArchive(tmp_path / "store")
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    dev_volume = prepare_volume(tmp_path / "volumes", "b1", "5", archive)
    (dev_volume / "projects").mkdir()
    (dev_volume / "projects" / "dev.jsonl").write_text('{"dev":1}\n', encoding="utf-8")
    harvest_volume(dev_volume, BurstRecord(burst_id="b1", task_id="5"), archive, ledger)

    reviewer_volume = prepare_volume(tmp_path / "volumes", "b2", "5", archive, seed=False)
    assert not any(reviewer_volume.iterdir())
