"""Session-volume choreography: materialize before boot, harvest after exit.

The burst container mounts one control-plane-owned directory as its
``CLAUDE_CONFIG_DIR``. Before boot we materialize the task's archived session
into it (so ``resume`` finds the transcript locally); after the burst we read
the result file, fold the violations feed into the ledger, and archive the
whole volume. The agent needs no storage credentials at any point.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from dev_agent.env import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME

from control_plane.models import BurstRecord, BurstStatus, Violation
from control_plane.store import Ledger, SessionArchive


@dataclass(slots=True)
class HarvestOutcome:
    result: dict | None
    violation_count: int
    transcript_key: str | None
    transcript_checksum: str | None


def prepare_volume(volumes_root: Path, burst_id: str, task_id: str, archive: SessionArchive) -> Path:
    """A fresh volume for this burst, seeded with the task's session if one exists."""
    volume = volumes_root / burst_id
    if volume.exists():
        shutil.rmtree(volume)
    volume.mkdir(parents=True)
    archive.get(task_id, volume)
    return volume


def harvest_volume(
    volume: Path, burst: BurstRecord, archive: SessionArchive, ledger: Ledger
) -> HarvestOutcome:
    """Fold a finished burst's volume back into the platform stores."""
    result: dict | None = None
    result_path = volume / BURST_RESULT_FILENAME
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        burst.session_id = result.get("session_id") or burst.session_id
        burst.model = result.get("model") or burst.model  # resolved beats requested
        burst.cost_usd = result.get("total_cost_usd")
        burst.input_tokens = result.get("input_tokens")
        burst.output_tokens = result.get("output_tokens")
        burst.num_turns = result.get("num_turns")
        if result.get("is_error"):
            burst.status = BurstStatus.FAILED

    count = 0
    violations_path = volume / VIOLATIONS_FILENAME
    if violations_path.is_file():
        for line in violations_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            ledger.record_violation(
                Violation(
                    task_id=burst.task_id,
                    burst_id=burst.burst_id,
                    tool=entry.get("tool", ""),
                    reason=entry.get("reason", ""),
                    detail=entry.get("input", ""),
                )
            )
            count += 1

    key, checksum = archive.put(burst.task_id, volume)
    return HarvestOutcome(
        result=result, violation_count=count, transcript_key=key, transcript_checksum=checksum
    )
