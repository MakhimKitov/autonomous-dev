"""The M2 reconciler: state machine transitions, rails, kill switch, recovery.

GitHub is a recording fake; execute_burst is monkeypatched — these tests drive
the decision layer only (the burst choreography has its own tests).
"""

from pathlib import Path

import pytest
from control_plane import scheduler as sched_mod
from control_plane.bursts import BurstOutcome, PlatformStores
from control_plane.github_api import COPILOT_REVIEWER, Issue, PullRequest, Review
from control_plane.models import BurstRecord, BurstStatus, TaskRecord, TaskStatus, utc_now
from control_plane.scheduler import Rails, Scheduler
from control_plane.volume import HarvestOutcome

OWNER = "owner1"


class FakeApi:
    def __init__(self):
        self.ready_issues = []
        self.issues = {}
        self.prs = {}
        self.open_prs = []
        self.reviews = {}
        self.review_comments = {}
        self.label_ops = []
        self.comments = []
        self.rerequests = []

    def list_ready_issues(self):
        return list(self.ready_issues)

    def get_issue(self, number):
        return self.issues.get(number) or Issue(number=number, title="t", body="b", labels=["human"])

    def add_labels(self, number, labels):
        self.label_ops.append(("add", number, tuple(labels)))

    def remove_label(self, number, label):
        self.label_ops.append(("remove", number, label))

    def comment(self, number, body):
        self.comments.append((number, body))

    def open_pr_with_branch_prefix(self, prefix):
        return next((pr for pr in self.open_prs if pr.head_ref.startswith(prefix)), None)

    def get_pr(self, number):
        return self.prs[number]

    def list_reviews(self, pr_number):
        return self.reviews.get(pr_number, [])

    def list_review_comments(self, pr_number):
        return self.review_comments.get(pr_number, [])

    def request_reviewers(self, pr_number, reviewers):
        self.rerequests.append((pr_number, tuple(reviewers)))


def _outcome(exit_code=0, is_error=False):
    return BurstOutcome(
        burst=BurstRecord(burst_id="fake", task_id="1", status=BurstStatus.SUCCEEDED),
        harvest=HarvestOutcome(
            result={"is_error": is_error}, violation_count=0, transcript_key="k", transcript_checksum="c"
        ),
        exit_code=exit_code,
        log_path=Path("/dev/null"),
    )


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    stores = PlatformStores.open(tmp_path / "data")
    api = FakeApi()
    rails = Rails(max_attempts=2, max_review_rounds=2, failure_cooldown_s=0)
    scheduler = Scheduler(stores, api, tmp_path / "repo", OWNER, rails=rails)
    monkeypatch.setattr(Scheduler, "_container_alive", lambda self, burst_id: False)
    spawned = []

    def fake_execute(stores_, task, repo, prompt, **kwargs):
        spawned.append({"task": task.task_id, "prompt": prompt, "kind": kwargs.get("kind")})
        return fake_execute.next_outcome

    fake_execute.next_outcome = _outcome()
    monkeypatch.setattr(sched_mod, "execute_burst", fake_execute)
    return scheduler, api, stores, spawned, fake_execute


def _issue(number=1, labels=("ready", "human")):
    return Issue(number=number, title="add /coinflip", body="do it", labels=list(labels))


def _pr(number=2, state="open", merged=False, head_ref="dev/1-coinflip"):
    return PullRequest(number=number, state=state, merged=merged, head_ref=head_ref, head_sha="abc")


def _changes_requested(review_id=10, author=OWNER):
    return Review(id=review_id, author=author, state="CHANGES_REQUESTED", body="tweak it")


def test_claims_ready_issue_and_swaps_labels(rig):
    scheduler, api, stores, _, _ = rig
    api.ready_issues = [_issue()]
    scheduler.observe()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "work"
    assert ("remove", 1, "ready") in api.label_ops
    assert ("add", 1, ("in-progress",)) in api.label_ops

    scheduler.observe()  # label drift: already claimed → no double claim
    assert api.label_ops.count(("remove", 1, "ready")) == 1


def test_work_burst_to_awaiting_review(rig):
    scheduler, api, stores, spawned, _ = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = [_pr()]
    scheduler.tick()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.AWAITING_REVIEW
    assert task.pr_number == 2 and task.next_burst_kind is None
    assert spawned[0]["kind"] == "work"
    assert "trusted intent" in spawned[0]["prompt"]  # human provenance framing


def test_failed_bursts_retry_then_block(rig):
    scheduler, api, stores, spawned, fake_execute = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    fake_execute.next_outcome = _outcome(exit_code=1)

    scheduler.tick()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1  # retry queued

    api.ready_issues = []
    scheduler.tick()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.BLOCKED and task.attempts == 2  # rail tripped
    assert any("failed attempts" in body for _, body in api.comments)
    assert ("remove", 1, "in-progress") in api.label_ops
    assert len(spawned) == 2


def test_success_without_pr_is_a_failed_attempt(rig):
    scheduler, api, stores, _, _ = rig
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.open_prs = []  # burst "succeeded" but the contract is a PR
    scheduler.tick()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1


def test_owner_changes_requested_triggers_review_round(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(task_id="1", repo="r", status=TaskStatus.AWAITING_REVIEW, pr_number=2)
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_changes_requested()]
    scheduler.observe()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.CLAIMED and task.next_burst_kind == "review"
    assert task.review_rounds == 1


def test_copilot_and_seen_reviews_never_trigger_rounds(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(
            task_id="1", repo="r", status=TaskStatus.AWAITING_REVIEW, pr_number=2, last_review_id=10
        )
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [
        _changes_requested(review_id=10),  # already processed (cursor)
        _changes_requested(review_id=11, author=COPILOT_REVIEWER),  # advisory
    ]
    scheduler.observe()
    assert stores.ledger.get_task("1").status == TaskStatus.AWAITING_REVIEW


def test_review_burst_advances_cursor_and_re_requests(rig):
    scheduler, api, stores, spawned, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(
            task_id="1",
            repo="r",
            status=TaskStatus.CLAIMED,
            pr_number=2,
            next_burst_kind="review",
            review_rounds=1,
        )
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr()
    api.reviews[2] = [_changes_requested(review_id=10)]
    scheduler.tick()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.AWAITING_REVIEW
    assert task.last_review_id == 10  # advanced only on success
    assert api.rerequests == [(2, (COPILOT_REVIEWER, OWNER))]  # the M1 gap, deterministic
    assert spawned[0]["kind"] == "review"
    assert "tweak it" in spawned[0]["prompt"]


def test_merged_pr_completes_the_task(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(task_id="1", repo="r", status=TaskStatus.AWAITING_REVIEW, pr_number=2)
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr(state="closed", merged=True)
    scheduler.observe()
    assert stores.ledger.get_task("1").status == TaskStatus.DONE
    assert ("remove", 1, "in-progress") in api.label_ops


def test_pr_closed_unmerged_blocks(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(task_id="1", repo="r", status=TaskStatus.AWAITING_REVIEW, pr_number=2)
    )
    api.issues[1] = _issue(labels=("in-progress",))
    api.prs[2] = _pr(state="closed", merged=False)
    scheduler.observe()
    assert stores.ledger.get_task("1").status == TaskStatus.BLOCKED
    assert any("closed without merge" in body for _, body in api.comments)


def test_agent_blocked_label_parks_the_task(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(TaskRecord(task_id="1", repo="r", status=TaskStatus.CLAIMED))
    api.issues[1] = _issue(labels=("in-progress", "blocked"))
    scheduler.observe()
    assert stores.ledger.get_task("1").status == TaskStatus.BLOCKED


def test_stop_flag_freezes_the_tick(rig):
    scheduler, api, stores, spawned, _ = rig
    api.ready_issues = [_issue()]
    scheduler.stop_path.touch()
    scheduler.tick()
    assert stores.ledger.get_task("1") is None  # nothing claimed
    assert not spawned and not api.label_ops


def test_reconcile_rolls_back_a_crashed_burst(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(task_id="1", repo="r", status=TaskStatus.WORKING, next_burst_kind="work")
    )
    crashed = BurstRecord(burst_id="dead1", task_id="1")  # status: running
    stores.ledger.record_burst(crashed)
    scheduler.reconcile_running()
    assert stores.ledger.running_bursts() == []
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.CLAIMED and task.attempts == 1
    assert task.next_burst_kind == "work"  # retries the same kind


def test_failure_cooldown_defers_retry(rig):
    scheduler, _, stores, _, _ = rig
    scheduler.rails.failure_cooldown_s = 3600
    stores.ledger.upsert_task(
        TaskRecord(
            task_id="1", repo="r", status=TaskStatus.CLAIMED, attempts=1, last_burst_at=utc_now()
        )
    )
    assert scheduler.pick_next() is None
    stores.ledger.upsert_task(
        TaskRecord(
            task_id="1",
            repo="r",
            status=TaskStatus.CLAIMED,
            attempts=1,
            last_burst_at="2020-01-01T00:00:00+00:00",
        )
    )
    assert scheduler.pick_next() is not None


def test_daily_ceiling_stops_spawning(rig):
    scheduler, _, stores, _, _ = rig
    scheduler.rails.daily_burst_ceiling = 0
    stores.ledger.upsert_task(TaskRecord(task_id="1", repo="r", status=TaskStatus.CLAIMED))
    assert scheduler.pick_next() is None


def test_re_ready_resets_rails_and_resumes_review_path(rig):
    scheduler, api, stores, _, _ = rig
    stores.ledger.upsert_task(
        TaskRecord(
            task_id="1",
            repo="r",
            status=TaskStatus.BLOCKED,
            pr_number=2,
            attempts=2,
            review_rounds=2,
        )
    )
    api.ready_issues = [_issue()]
    api.issues[1] = _issue()
    api.prs[2] = _pr()  # the PR is still open → resume as a review round
    scheduler.observe()
    task = stores.ledger.get_task("1")
    assert task.status == TaskStatus.CLAIMED
    assert task.attempts == 0 and task.review_rounds == 0
    assert task.next_burst_kind == "review"


def test_rails_from_env_overrides():
    rails = Rails.from_env({"SCHEDULER_MAX_ATTEMPTS": "5", "SCHEDULER_BURST_BUDGET_USD": "0.5"})
    assert rails.max_attempts == 5
    assert rails.burst_budget_usd == 0.5
    assert rails.max_review_rounds == 3  # untouched default
