"""The M2 reconciler (SCHEDULER.md): observe GitHub, advance tasks, run bursts.

Task state lives on ``TaskRecord.status``; labels are reflective only. Serial
by design — one burst globally. Each tick re-derives every decision from
GitHub + the ledger, so a killed or crashed scheduler resumes cleanly.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from control_plane.bursts import PlatformStores, execute_burst
from control_plane.github_api import COPILOT_REVIEWER, GitHubApi, GitHubApiError, Review
from control_plane.github_auth import GitHubAuthError
from control_plane.models import BurstStatus, TaskRecord, TaskStatus, utc_now
from control_plane.prompts import review_prompt, work_prompt
from control_plane.runner import DEFAULT_IMAGE

STOP_FILENAME = "STOP"

WORK = "work"
REVIEW = "review"


@dataclass(slots=True)
class Rails:
    """Hard limits on unattended behavior. Defaults per SCHEDULER.md."""

    max_attempts: int = 3
    max_review_rounds: int = 3
    daily_burst_ceiling: int = 20
    failure_cooldown_s: int = 300
    poll_interval_s: int = 60
    burst_budget_usd: float = 2.0
    burst_max_turns: int = 100

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Rails:
        env = dict(os.environ) if environ is None else environ
        defaults = cls()

        def _get(name: str, cast, fallback):
            raw = env.get(name)
            return cast(raw) if raw else fallback

        return cls(
            max_attempts=_get("SCHEDULER_MAX_ATTEMPTS", int, defaults.max_attempts),
            max_review_rounds=_get("SCHEDULER_MAX_REVIEW_ROUNDS", int, defaults.max_review_rounds),
            daily_burst_ceiling=_get("SCHEDULER_DAILY_BURST_CEILING", int, defaults.daily_burst_ceiling),
            failure_cooldown_s=_get("SCHEDULER_FAILURE_COOLDOWN_S", int, defaults.failure_cooldown_s),
            poll_interval_s=_get("SCHEDULER_POLL_INTERVAL_S", int, defaults.poll_interval_s),
            burst_budget_usd=_get("SCHEDULER_BURST_BUDGET_USD", float, defaults.burst_budget_usd),
            burst_max_turns=_get("SCHEDULER_BURST_MAX_TURNS", int, defaults.burst_max_turns),
        )


class Scheduler:
    def __init__(
        self,
        stores: PlatformStores,
        api: GitHubApi,
        repo: Path,
        owner: str,
        rails: Rails | None = None,
        image: str = DEFAULT_IMAGE,
        model: str | None = None,
    ) -> None:
        self.stores = stores
        self.api = api
        self.repo = repo
        self.owner = owner  # the human whose changes-requested reviews trigger rounds
        self.rails = rails or Rails()
        self.image = image
        self.model = model
        self.current_burst_id: str | None = None  # for the kill switch mid-burst

    # --- stop flag ---

    @property
    def stop_path(self) -> Path:
        return self.stores.data_dir / STOP_FILENAME

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    # --- the tick ---

    def tick(self) -> None:
        if self.stop_requested():
            return
        self.reconcile_running()
        self.observe()
        if self.stop_requested():  # `stop` may have landed during observation
            return
        task = self.pick_next()
        if task is not None:
            self.run_task_burst(task)

    # --- crash recovery ---

    def reconcile_running(self) -> None:
        """Ledger says running, docker says gone → the scheduler died mid-burst."""
        for burst in self.stores.ledger.running_bursts():
            if self._container_alive(burst.burst_id):
                continue
            burst.status = BurstStatus.KILLED
            burst.finished_at = utc_now()
            self.stores.ledger.finish_burst(burst)
            print(f"[scheduler] reconcile: burst {burst.burst_id} died without a result → killed")
        # a WORKING task with no live burst is the same crash, seen from the task side
        for task in self.stores.ledger.tasks_by_status(TaskStatus.WORKING):
            self._register_failure(task, "the scheduler crashed or was killed mid-burst")

    def _container_alive(self, burst_id: str) -> bool:
        out = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"name=burst-{burst_id}"],
            capture_output=True,
            text=True,
        )
        return bool(out.stdout.strip())

    # --- observation: GitHub facts → state transitions ---

    def observe(self) -> None:
        self._claim_ready_issues()
        self._park_blocked_issues()
        self._advance_awaiting_review()

    def _claim_ready_issues(self) -> None:
        for issue in self.api.list_ready_issues():
            task = self.stores.ledger.get_task(str(issue.number))
            if task is not None and task.status in (
                TaskStatus.CLAIMED,
                TaskStatus.WORKING,
                TaskStatus.AWAITING_REVIEW,
            ):
                continue  # already ours; the label is drift
            if task is None:
                task = TaskRecord(task_id=str(issue.number), repo=str(self.repo))
            task.status = TaskStatus.CLAIMED
            task.attempts = 0  # a human re-`ready` is a fresh grant
            task.review_rounds = 0
            task.next_burst_kind = WORK
            if task.pr_number:  # re-readied with a PR still open → resume the review path
                pr = self.api.get_pr(task.pr_number)
                if pr.state == "open" and not pr.merged:
                    task.next_burst_kind = REVIEW
            self.stores.ledger.upsert_task(task)
            self.api.remove_label(issue.number, "ready")
            self.api.add_labels(issue.number, ["in-progress"])
            print(f"[scheduler] claimed issue #{issue.number} ({issue.provenance}) → {task.next_burst_kind} burst")

    def _park_blocked_issues(self) -> None:
        """The agent set `blocked` (contract rule 9) — stop scheduling the task."""
        for task in self.stores.ledger.tasks_by_status(TaskStatus.CLAIMED, TaskStatus.AWAITING_REVIEW):
            issue = self.api.get_issue(int(task.task_id))
            if "blocked" in issue.labels:
                task.status = TaskStatus.BLOCKED
                task.next_burst_kind = None
                self.stores.ledger.upsert_task(task)
                print(f"[scheduler] issue #{task.task_id} is `blocked` — parked for a human")

    def _advance_awaiting_review(self) -> None:
        for task in self.stores.ledger.tasks_by_status(TaskStatus.AWAITING_REVIEW):
            if not task.pr_number:
                continue
            number = int(task.task_id)
            pr = self.api.get_pr(task.pr_number)
            if pr.merged:
                task.status = TaskStatus.DONE
                task.next_burst_kind = None
                self.stores.ledger.upsert_task(task)
                self.api.remove_label(number, "in-progress")
                print(f"[scheduler] PR #{task.pr_number} merged — task #{task.task_id} done")
                continue
            if pr.state == "closed":
                task.status = TaskStatus.BLOCKED
                task.next_burst_kind = None
                self.stores.ledger.upsert_task(task)
                self.api.comment(number, "⛔ control plane: PR closed without merge — parking this task for a human.")
                self.api.remove_label(number, "in-progress")
                print(f"[scheduler] PR #{task.pr_number} closed unmerged — task #{task.task_id} blocked")
                continue
            if not self._unseen_owner_changes(task):
                continue
            if task.review_rounds >= self.rails.max_review_rounds:
                task.status = TaskStatus.BLOCKED
                task.next_burst_kind = None
                self.stores.ledger.upsert_task(task)
                self.api.comment(
                    number,
                    f"⛔ control plane: parking this task after {task.review_rounds} review rounds — "
                    "a human can re-add `ready` to grant more.",
                )
                self.api.remove_label(number, "in-progress")
                continue
            task.review_rounds += 1
            task.status = TaskStatus.CLAIMED
            task.next_burst_kind = REVIEW
            self.stores.ledger.upsert_task(task)
            print(f"[scheduler] changes requested on PR #{task.pr_number} — review round {task.review_rounds}")

    def _unseen_owner_changes(self, task: TaskRecord) -> list[Review]:
        """Owner changes-requested reviews above the processed cursor.

        The cursor (last_review_id) advances only after a review burst
        succeeds, so failed rounds re-surface the same reviews. Copilot
        reviews never trigger a round — advisory only.
        """
        reviews = self.api.list_reviews(task.pr_number)
        last = task.last_review_id or 0
        return [
            review
            for review in reviews
            if review.id > last and review.state == "CHANGES_REQUESTED" and review.author == self.owner
        ]

    # --- spawning ---

    def pick_next(self) -> TaskRecord | None:
        claimed = self.stores.ledger.tasks_by_status(TaskStatus.CLAIMED)
        if not claimed:
            return None
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if self.stores.ledger.bursts_started_since(day_start) >= self.rails.daily_burst_ceiling:
            print("[scheduler] rails: daily burst ceiling reached — not spawning")
            return None
        now = datetime.now(UTC)
        for task in claimed:  # oldest first (query order)
            if task.attempts and task.last_burst_at:
                cooled_at = datetime.fromisoformat(task.last_burst_at) + timedelta(
                    seconds=self.rails.failure_cooldown_s
                )
                if now < cooled_at:
                    continue
            return task
        return None

    def run_task_burst(self, task: TaskRecord) -> None:
        number = int(task.task_id)
        kind = task.next_burst_kind or WORK
        if kind == WORK:
            prompt = work_prompt(self.api.get_issue(number))
            pending: list[Review] = []
        else:
            pending = self._unseen_owner_changes(task)
            if not pending:  # review dismissed/vanished between ticks
                task.status = TaskStatus.AWAITING_REVIEW
                self.stores.ledger.upsert_task(task)
                return
            prompt = review_prompt(task.pr_number, pending, self.api.list_review_comments(task.pr_number))

        task.status = TaskStatus.WORKING
        task.last_burst_at = utc_now()
        self.stores.ledger.upsert_task(task)

        self.current_burst_id = uuid.uuid4().hex[:12]
        try:
            outcome = execute_burst(
                self.stores,
                task,
                self.repo,
                prompt,
                kind=kind,
                image=self.image,
                model=self.model,
                max_budget_usd=self.rails.burst_budget_usd,
                max_turns=self.rails.burst_max_turns,
                burst_id=self.current_burst_id,
            )
        except (GitHubAuthError, GitHubApiError) as exc:
            print(f"[scheduler] burst spawn failed: {exc}")
            self._register_failure(task, f"burst could not start: {exc}")
            return
        finally:
            self.current_burst_id = None

        if not outcome.succeeded:
            self._register_failure(task, f"burst {outcome.burst.burst_id} failed (see its log)")
            return

        if kind == WORK:
            pr = self.api.open_pr_with_branch_prefix(f"dev/{number}-")
            if pr is None:
                # the work burst's contract is a PR; anything less is a failed attempt
                self._register_failure(task, f"burst {outcome.burst.burst_id} succeeded but opened no PR")
                return
            task.pr_number = pr.number
            print(f"[scheduler] task #{task.task_id}: PR #{pr.number} open")
            self._request_re_review(task)  # Copilot auto-review is not reliable on open
        else:
            task.last_review_id = max(review.id for review in pending)
            self._request_re_review(task)
        task.status = TaskStatus.AWAITING_REVIEW
        task.next_burst_kind = None
        self.stores.ledger.upsert_task(task)

    def _request_re_review(self, task: TaskRecord) -> None:
        """Deterministic review request: on PR open (auto-review observed not
        firing) and after review-round pushes (the M1 gap). Never fatal."""
        try:
            self.api.request_reviewers(task.pr_number, [COPILOT_REVIEWER, self.owner])
            print(f"[scheduler] re-requested review on PR #{task.pr_number}: Copilot + {self.owner}")
        except GitHubApiError as exc:
            print(f"[scheduler] re-request failed (advisory, continuing): {exc}")

    # --- failure rail ---

    def _register_failure(self, task: TaskRecord, reason: str) -> None:
        task.attempts += 1
        task.last_burst_at = utc_now()
        if task.attempts >= self.rails.max_attempts:
            task.status = TaskStatus.BLOCKED
            task.next_burst_kind = None
            number = int(task.task_id)
            self.api.comment(
                number,
                f"⛔ control plane: parking this task after {task.attempts} failed attempts — {reason}. "
                "A human can re-add `ready` to retry.",
            )
            self.api.remove_label(number, "in-progress")
            print(f"[scheduler] task #{task.task_id} blocked after {task.attempts} attempts: {reason}")
        else:
            task.status = TaskStatus.CLAIMED  # next_burst_kind unchanged — retry the same kind
            print(f"[scheduler] task #{task.task_id} attempt {task.attempts} failed: {reason} — will retry")
        self.stores.ledger.upsert_task(task)
