"""`execute_burst`'s two task writes, in the E1 shapes.

The engine is shared by the hand-run verb (a task the ledger may never have
seen) and the scheduler (a task it holds leased and `working`). It creates the
row if absent — never overwrites one that exists — and lands the dev's session
id by compare-and-set against the (status, holder) the task carried on entry.
"""

import json
from datetime import UTC, datetime

from control_plane.bursts import PlatformStores, execute_burst
from control_plane.models import BurstStatus, TaskRecord, TaskStatus, utc_now
from control_plane.subscriptions import declare
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


# --- F3: a spent token benches its subscription, and the session survives ---


class RateLimitedRunner(ResultRunner):
    """Docker's seat when the provider says no: the worker still writes its
    result — with the session id from the init message — and exits non-zero."""

    def __init__(self, session_id="sess-9", message="Claude AI usage limit reached|%d"):
        super().__init__(session_id)
        self.message = message

    def run(self, spec, log_path=None, heartbeat=None):
        reset = int(datetime.now(UTC).timestamp()) + 3600
        (spec.volume_dir / BURST_RESULT_FILENAME).write_text(
            json.dumps({
                "session_id": self.session_id,
                "is_error": True,
                "error": self.message % reset if "%d" in self.message else self.message,
            }),
            encoding="utf-8",
        )
        return 1


def test_a_rate_limited_burst_benches_the_subscription_that_paid(tmp_path, monkeypatch):
    """F3: the token is spent, so the row that named it is benched by CAS until
    the provider's own reset — and the outcome says so, because the rail that
    reads it must not charge the task for a limit it did not cause."""
    stores, repo = _rig(tmp_path)
    monkeypatch.setenv("ADA_TOKEN", "adas-secret")
    stores.ledger.register_subscription(declare("ada", "env:ADA_TOKEN", 1_000_000, "2027-09-01"), "op")

    outcome = execute_burst(stores, _task(), repo, "fix", runner=RateLimitedRunner())

    assert outcome.succeeded is False and outcome.rate_limited is True
    row = stores.ledger.get_subscription("ada")
    assert row.backoff_until == outcome.rate_limited_until > utc_now()
    assert row.status == "active"  # benched, not ended: the member did nothing wrong
    assert stores.ledger.get_burst(outcome.burst.burst_id).subscription_id == "ada"
    # and the session is on the task, so the retry on another token resumes it
    assert stores.ledger.get_task(f"{REPOSITORY}#7").session_id == "sess-9"


def test_an_ordinary_failure_benches_nobody(tmp_path, monkeypatch):
    stores, repo = _rig(tmp_path)
    monkeypatch.setenv("ADA_TOKEN", "adas-secret")
    stores.ledger.register_subscription(declare("ada", "env:ADA_TOKEN", 1_000_000, "2027-09-01"), "op")

    outcome = execute_burst(
        stores, _task(), repo, "fix",
        runner=RateLimitedRunner(message="TypeError: the agent tripped over its own feet"),
    )
    assert outcome.succeeded is False and outcome.rate_limited is False
    assert stores.ledger.get_subscription("ada").backoff_until is None


def test_a_refused_credential_expires_the_subscription(tmp_path, monkeypatch):
    """F4: the token itself was refused — the year ran out, or the member
    revoked it at the provider. The row says `expired` from now on, with an
    audit row, because the platform changed a member's standing on its own
    authority; the task is not blamed for it."""
    stores, repo = _rig(tmp_path)
    monkeypatch.setenv("ADA_TOKEN", "adas-secret")
    stores.ledger.register_subscription(declare("ada", "env:ADA_TOKEN", 1_000_000, "2027-09-01"), "op")

    outcome = execute_burst(
        stores, _task(), repo, "fix",
        runner=RateLimitedRunner(message="AuthenticationError: invalid api key"),
    )
    assert outcome.succeeded is False and outcome.rate_limited is False
    row = stores.ledger.get_subscription("ada")
    assert row.status == "expired" and row.backoff_until is None
    assert [a.action for a in stores.ledger.audit_trail("ada")] == [
        "subscription.register", "subscription.expire",
    ]
    assert [e.kind for e in stores.ledger.events() if e.kind.startswith("subscription_")] == [
        "subscription_registered", "subscription_expired",
    ]


# --- E3: a burst the heartbeat fenced is `killed`, not `failed` ---


class FencedRunner(ResultRunner):
    """Docker's seat when the heartbeat says no: the container is killed and
    its exit code comes back — 137, the same number a crash would leave."""

    def run(self, spec, log_path=None, heartbeat=None):
        assert heartbeat() is False
        return 137


class CrashedRunner(ResultRunner):
    def run(self, spec, log_path=None, heartbeat=None):
        assert heartbeat() is True
        return 137


def test_a_fenced_burst_is_killed_and_a_crashed_one_failed(tmp_path):
    """The exit code cannot tell a kill from a crash — both are 137 — so the
    engine remembers the heartbeat's answer: fenced (the lease lost, the org
    stopped, the replica shutting down) is `killed`; the same exit without a
    fence is `failed`. Neither counts as success."""
    stores, repo = _rig(tmp_path)
    task = _task(status=TaskStatus.WORKING, claimed_by="r1")
    stores.ledger.upsert_task(task)

    fenced = execute_burst(stores, task, repo, "fix", runner=FencedRunner(), heartbeat=lambda: False)
    assert (fenced.burst.status, fenced.exit_code, fenced.succeeded) == (BurstStatus.KILLED, 137, False)
    assert stores.ledger.get_burst(fenced.burst.burst_id).status == BurstStatus.KILLED

    crashed = execute_burst(stores, task, repo, "fix", runner=CrashedRunner(), heartbeat=lambda: True)
    assert (crashed.burst.status, crashed.exit_code, crashed.succeeded) == (BurstStatus.FAILED, 137, False)
