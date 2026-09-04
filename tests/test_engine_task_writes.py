"""`execute_burst`'s two task writes, in the E1 shapes.

The engine is shared by the hand-run verb (a task the ledger may never have
seen) and the scheduler (a task it holds leased and `working`). It creates the
row if absent — never overwrites one that exists — and lands the dev's session
id by compare-and-set against the (status, holder) the task carried on entry.
"""

import json

from control_plane.bursts import PlatformStores, execute_burst
from control_plane.models import TaskRecord, TaskStatus
from dev_agent.env import BURST_RESULT_FILENAME

REPOSITORY = "owner/product"


class ResultRunner:
    """Docker's seat: writes the worker's result file, exits 0."""

    def __init__(self, session_id="sess-9"):
        self.session_id = session_id

    def run(self, spec, log_path=None, heartbeat=None):
        (spec.volume_dir / BURST_RESULT_FILENAME).write_text(
            json.dumps({"session_id": self.session_id, "model": "m", "is_error": False}), encoding="utf-8"
        )
        return 0


def _task(**overrides):
    return TaskRecord(task_id=f"{REPOSITORY}#7", repository=REPOSITORY, issue_number=7, **overrides)


def _rig(tmp_path):
    stores = PlatformStores.open(tmp_path / "data")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return stores, repo


def test_an_unseen_task_is_created_and_a_seen_one_is_left_as_it_is(tmp_path):
    stores, repo = _rig(tmp_path)
    assert stores.ledger.get_task(f"{REPOSITORY}#7") is None
    execute_burst(stores, _task(), repo, "fix", runner=ResultRunner())
    created = stores.ledger.get_task(f"{REPOSITORY}#7")
    assert created.status == TaskStatus.OPEN  # the verb's shape: created, as handed in

    # the scheduler's shape: the row exists, leased and working, with state the
    # in-memory record may lag behind — the engine must not write it back
    stores.ledger.upsert_task(
        _task(status=TaskStatus.WORKING, attempts=2, pr_number=4, claimed_by="r1", claimed_until="2099-01-01T00:00:00+00:00")
    )
    stale = _task(status=TaskStatus.WORKING, attempts=0, pr_number=None, claimed_by="r1")
    execute_burst(stores, stale, repo, "fix", runner=ResultRunner("sess-10"))
    row = stores.ledger.get_task(f"{REPOSITORY}#7")
    assert (row.status, row.attempts, row.pr_number, row.claimed_by) == (TaskStatus.WORKING, 2, 4, "r1")
    assert row.session_id == "sess-10"  # the one column the engine owns landed


def test_the_dev_session_lands_by_cas_and_a_moved_task_is_left_alone(tmp_path):
    stores, repo = _rig(tmp_path)
    task = _task(status=TaskStatus.WORKING, claimed_by="r1")
    stores.ledger.upsert_task(task)

    class PeerMovesItRunner(ResultRunner):
        def run(self, spec, log_path=None, heartbeat=None):
            # while the container ran, a peer reclaimed the task (our lease had lapsed)
            stores.ledger.transition_task(
                task.task_id, {"status": TaskStatus.CLAIMED, "claimed_by": None, "claimed_until": None},
                expected_status=TaskStatus.WORKING, expected_holder="r1",
            )
            return super().run(spec, log_path, heartbeat)

    outcome = execute_burst(stores, task, repo, "fix", runner=PeerMovesItRunner())
    assert outcome.burst.session_id == "sess-9"  # the burst row knows its session
    row = stores.ledger.get_task(task.task_id)
    assert (row.status, row.session_id) == (TaskStatus.CLAIMED, None)  # the peer's row, untouched
    assert task.session_id is None  # and the record was not told otherwise

    # the reviewer's session is never the task's, CAS or not
    execute_burst(stores, _task(status=TaskStatus.CLAIMED), repo, "judge", role="reviewer", fresh=True,
                  runner=ResultRunner("sess-reviewer"))
    assert stores.ledger.get_task(task.task_id).session_id is None
