"""Session-volume choreography: materialize before boot, harvest after exit.

The burst container mounts one control-plane-owned directory as its
``CLAUDE_CONFIG_DIR``. Before boot we materialize the parent burst's snapshot
into it (so ``resume`` finds the transcript locally); after the burst we read the
result file, fold the violations feed into the ledger, and archive the whole
volume under this burst's own key. The agent needs no storage credentials at any
point.

Since A5 the two halves are a tree walk rather than a blob swap: ``prepare``
descends from the leaf the ledger names, ``harvest`` writes a new leaf, and the
edge between them is ``resumed_from_burst_id``. Retry re-materializes the same
parent; nothing in the archive is ever rewritten (STORES.md, "The session
archive").
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from dev_agent.env import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME

from control_plane.models import BurstRecord, BurstStatus, Violation
from control_plane.store import Ledger, SessionArchive, session_key


@dataclass(slots=True)
class HarvestOutcome:
    result: dict | None
    violation_count: int
    session_key: str | None
    session_checksum: str | None


def prepare_volume(
    volumes_root: Path,
    burst_id: str,
    archive: SessionArchive,
    parent: BurstRecord | None = None,
) -> Path:
    """A fresh volume for this burst, materialized from its parent node.

    ``parent`` is the leaf of the task's session tree — the burst whose snapshot
    this one continues from (A5). ``None`` means start empty, which covers both
    honest cases with one parameter: a task's first burst has no parent, and a
    deliberately-amnesiac burst is given none. The reviewer is the second kind, and
    the property it needs is unchanged — the dev's transcripts must not be
    *readable* from inside, not merely unresumed — but it is now impossible to ask
    for a parent and not materialize it.

    The checksum comes from the ledger's row, never from the archive: the archive
    is asked to prove the bytes match what the ledger recorded, and a mismatch
    raises before anything reaches the volume.
    """
    volume = volumes_root / burst_id
    if volume.exists():
        shutil.rmtree(volume)
    volume.mkdir(parents=True)
    if parent is not None and parent.session_key:
        archive.get(parent.session_key, volume, parent.session_checksum or "")
    return volume


def harvest_volume(
    volume: Path,
    burst: BurstRecord,
    archive: SessionArchive,
    ledger: Ledger,
    archive_session: bool = True,
) -> HarvestOutcome:
    """Fold a finished burst's volume back into the platform stores.

    ``archive_session=False`` for reviewer and tester bursts. Under the per-burst
    key scheme it can no longer *overwrite* anything — every burst has its own key
    — so the reason is the one STORES.md gives rather than the one the MVP had:
    their volumes are never seeded and never archived, because nothing resumes
    them and they are not nodes in the dev's session tree. The transcript still
    exists in the volume directory for the length of the run."""
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

    if archive_session:
        # The snapshot is this burst's own, keyed by it, and written once. Both
        # halves go on the burst record, which finish_burst persists — the ledger
        # is where a checksum is authoritative.
        burst.session_key = session_key(burst.task_id, burst.burst_id)
        burst.session_checksum = archive.put(burst.session_key, volume)
    return HarvestOutcome(
        result=result,
        violation_count=count,
        session_key=burst.session_key,
        session_checksum=burst.session_checksum,
    )
