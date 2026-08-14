"""Domain vocabulary: tasks, bursts, violations.

A *task* is one GitHub issue worked to completion; a *burst* is one container
run against it (work / review round / test). The control plane's records mirror
ARCHITECTURE.md §6 — state per task = branch (GitHub) + session (object store)
+ issue/PR thread (GitHub); these rows are the queryable index over it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Role(StrEnum):
    DEV = "dev"
    TESTER = "tester"


class BurstKind(StrEnum):
    WORK = "work"
    REVIEW = "review"
    TEST = "test"


class BurstStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    KILLED = "killed"


class TaskStatus(StrEnum):
    """The scheduler's state machine (SCHEDULER.md)."""

    OPEN = "open"  # manual driver (M0/M1) — outside the scheduler's machine
    CLAIMED = "claimed"  # ours; a burst is queued (see next_burst_kind)
    WORKING = "working"  # the burst container is running
    AWAITING_REVIEW = "awaiting_review"  # PR open; humans/Copilot have the ball
    BLOCKED = "blocked"  # needs a human
    DONE = "done"  # PR merged; issue auto-closed


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class TaskRecord:
    """One issue under work; keyed by the issue id."""

    task_id: str
    repo: str
    status: str = TaskStatus.OPEN
    session_id: str | None = None
    transcript_key: str | None = None
    transcript_checksum: str | None = None
    pr_number: int | None = None
    attempts: int = 0
    review_rounds: int = 0
    next_burst_kind: str | None = None  # work | review, while claimed/working
    last_review_id: int | None = None  # advanced only after a review burst succeeds
    last_burst_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class BurstRecord:
    """One container run — a row in the burst ledger."""

    burst_id: str
    task_id: str
    kind: str = BurstKind.WORK
    role: str = Role.DEV
    status: str = BurstStatus.RUNNING
    trace_id: str = ""
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    exit_code: int | None = None
    model: str | None = None  # requested at spawn, overwritten by the resolved model on harvest
    cost_usd: float | None = None  # advisory under subscription billing
    input_tokens: int | None = None
    output_tokens: int | None = None
    num_turns: int | None = None
    session_id: str | None = None


@dataclass(slots=True)
class Violation:
    """A wall block or tripwire firing — the experiment's primary dataset (§9)."""

    task_id: str
    burst_id: str
    tool: str
    reason: str
    detail: str = ""
    at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Event:
    """One outbox row: what the platform tells the outside world happened.

    Read by cursor (`id` is the consumer's position) and never by subscription —
    a push may wake a consumer early, the poll is the truth (STORES.md). The
    payload carries only what routing an alert needs; the row it announces is
    there to be read by whoever wants the rest.
    """

    id: int
    kind: str
    payload: dict
    created_at: str


# Event kinds, as they appear in `events.kind`. The set grows with the stages
# that emit them (capacity_exhausted at F2, subscription_expiring at F4,
# task_blocked with the scheduler's own events) — this is the one A4 ships,
# because the violations feed is the alert signal the platform documents
# (TO-BE §11: any new violations row is a page).
EVENT_VIOLATION_RECORDED = "violation_recorded"

# The optional wake channel. A consumer may LISTEN on it to poll sooner; nothing
# is allowed to depend on a notification arriving.
EVENT_CHANNEL = "events"
