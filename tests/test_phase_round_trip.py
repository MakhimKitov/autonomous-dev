"""G2's done-when: a burst's volume goes in and out of a real object store while
nothing beside the burst holds a credential for it (#43).

The pod, played by three processes. The control plane mints the capabilities;
`burst-init` and `burst-harvest` run as **subprocesses with nothing in their
environment but those URLs and paths** — no ARCHIVE_URL, no access key, no
region, nothing that would still open a door tomorrow; and between them a stand-in
for the agent writes what an agent writes. Then the control plane reads the
record back and folds it, and what it gets is the same `BurstArtifacts` a host
phase would have produced.

`ARCHIVE_URL` only: presigning is what an object store does and a directory
cannot, so this is the stores job's, skipped everywhere else.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from burst_phases.phases import (
    ARTIFACTS_PUT_URL,
    SESSION_GET_URL,
    SESSION_KEY,
    SESSION_PUT_URL,
    SESSION_SHA256,
    VOLUME_DIR,
)
from control_plane.models import BurstRecord, BurstStatus, TaskRecord
from control_plane.phases import artifacts_key, mint_phase_env, read_artifacts
from control_plane.store import ARCHIVE_URL_ENV, SqliteLedger, session_key
from control_plane.volume import Snapshot, fold_artifacts
from session_volume import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME

URL = os.environ.get(ARCHIVE_URL_ENV, "").strip()
pytestmark = pytest.mark.skipif(not URL, reason=f"{ARCHIVE_URL_ENV} unset — no bucket to presign against")

TASK = "owner/product#42"


@pytest.fixture
def archive():
    from control_plane.objectstore import ObjectSessionArchive

    backend = ObjectSessionArchive.from_url(URL)
    backend.prefix = f"{backend.prefix}/phases-{uuid4().hex[:8]}".strip("/")
    return backend


def phase(entrypoint: str, environ: dict[str, str]) -> subprocess.CompletedProcess:
    """One phase, as a container runs it: this environment and no other.

    `PATH` is there because `git` has to be findable and the interpreter has to
    start. Everything else the process could use to reach the store — the URL of
    the bucket, a key, a region, a profile — is deliberately absent, which is the
    whole claim being tested.
    """
    return subprocess.run(
        [sys.executable, "-c", f"import sys; from burst_phases.cli import {entrypoint}; sys.exit({entrypoint}())"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environ},
        capture_output=True,
        text=True,
    )


def an_agent_wrote(volume: Path) -> None:
    """What the box leaves behind: a transcript, a result, a violations feed."""
    (volume / "projects").mkdir(parents=True, exist_ok=True)
    (volume / "projects" / "sess.jsonl").write_text('{"turn": 2}\n', encoding="utf-8")
    (volume / BURST_RESULT_FILENAME).write_text(
        json.dumps({
            "session_id": "sess-round-trip",
            "model": "claude-sonnet-5-20260203",
            "is_error": False,
            "input_tokens": 1200,
            "output_tokens": 340,
            "num_turns": 7,
            "total_cost_usd": 0.5,
        }),
        encoding="utf-8",
    )
    (volume / VIOLATIONS_FILENAME).write_text(
        json.dumps({"tool": "Read", "reason": "path escapes the repository clone", "input": "/etc/os-release"}) + "\n",
        encoding="utf-8",
    )
    # and the thing that must never leave, whatever anyone passes
    (volume / ".credentials.json").write_text('{"token": "oat-must-not-travel"}', encoding="utf-8")


def test_a_burst_goes_in_and_out_through_capabilities_alone(archive, tmp_path):
    parent_volume = tmp_path / "parent"
    (parent_volume / "projects").mkdir(parents=True)
    (parent_volume / "projects" / "sess.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")
    parent_key = session_key(TASK, "b0")
    parent_checksum = archive.put(parent_key, parent_volume)

    volume = tmp_path / "volume"
    key = session_key(TASK, "b1")
    minted = mint_phase_env(
        archive,
        volume_dir=str(volume),
        parent=Snapshot(parent_key, parent_checksum),
        session_key=key,
    )

    # --- init: the parent's transcript is there before the agent boots ---
    started = phase("init_main", minted.init)
    assert started.returncode == 0, started.stderr
    assert (volume / "projects" / "sess.jsonl").read_text(encoding="utf-8") == '{"turn": 1}\n'

    # --- the agent, in between ---
    an_agent_wrote(volume)

    # --- harvest: everything the ledger will need leaves through a URL ---
    shipped = phase("harvest_main", minted.harvest)
    assert shipped.returncode == 0, shipped.stderr

    # --- and the control plane, which never touched the volume ---
    artifacts = read_artifacts(archive, artifacts_key(key))
    assert artifacts.result["session_id"] == "sess-round-trip"
    assert artifacts.violations[0]["tool"] == "Read"
    assert artifacts.session_key == key and len(artifacts.session_checksum) == 64

    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        burst = BurstRecord(burst_id="b1", task_id=TASK, status=BurstStatus.SUCCEEDED)
        ledger.upsert_task(TaskRecord(task_id=TASK, repository="owner/product", issue_number=42))
        outcome = fold_artifacts(artifacts, burst, ledger)
        assert outcome.violation_count == 1
        assert (burst.session_id, burst.input_tokens, burst.num_turns) == ("sess-round-trip", 1200, 7)
        assert burst.session_key == key and burst.session_checksum == artifacts.session_checksum
        assert [v.tool for v in ledger.violations()] == ["Read"]
    finally:
        ledger.close()

    # --- what the snapshot really holds: the work, never the credential ---
    restored = tmp_path / "restored"
    archive.get(key, restored, artifacts.session_checksum)
    assert (restored / "projects" / "sess.jsonl").read_text(encoding="utf-8") == '{"turn": 2}\n'
    assert not (restored / ".credentials.json").exists()


def test_the_phases_hold_no_key_to_the_store(archive, tmp_path):
    """The claim, made the only way it can be: the environment a phase is given
    is read back and searched. A capability names one object and expires; a
    credential would still be a credential tomorrow."""
    minted = mint_phase_env(
        archive, volume_dir=str(tmp_path / "volume"), parent=None, session_key=session_key(TASK, "b2")
    )
    handed = {**minted.init, **minted.harvest}
    for name, value in handed.items():
        assert "ACCESS_KEY" not in name and "SECRET" not in name, name
        if value.startswith("http"):
            assert "X-Amz-Expires=" in value or "Expires=" in value, f"{name} does not expire"
    # and the URLs are for this burst's objects only. The key travels
    # percent-encoded — a task id carries a `#`, which is a fragment in a URL —
    # so the comparison is against the encoded prefix, the way the store sees it.
    prefix = quote(session_key(TASK, "b2").rsplit("/", 1)[0], safe="/")
    assert all(prefix in value for name, value in handed.items() if value.startswith("http"))


def test_an_expired_capability_stops_working(archive, tmp_path):
    """Time-boxed to the rail: a URL minted for a burst is not a key to the
    store afterwards. One second, then the door is shut."""
    import time

    key = session_key(TASK, "b3")
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / BURST_RESULT_FILENAME).write_text("{}", encoding="utf-8")
    minted = mint_phase_env(archive, volume_dir=str(volume), parent=None, session_key=key, ttl_s=1)

    time.sleep(2)
    shipped = phase("harvest_main", minted.harvest)
    assert shipped.returncode == 1
    assert "refused" in shipped.stderr or "could not be written" in shipped.stderr
