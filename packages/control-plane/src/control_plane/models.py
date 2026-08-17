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
    # The MVP's one-blob-per-task pointer. Nothing writes these since A5 — the
    # snapshot lives on the burst that made it — and A6 (#17) backfills the blobs
    # they still name into the tree and drops them.
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
    """One container run — a row in the burst ledger.

    Also a node in the session tree (A5): ``resumed_from_burst_id`` names the
    parent this burst's volume was materialized from, and
    ``session_key``/``session_checksum`` name the snapshot this burst left behind.
    The ledger owns the tree; the archive is dumb storage that knows only keys.
    """

    burst_id: str
    task_id: str
    kind: str = BurstKind.WORK
    role: str = Role.DEV
    status: str = BurstStatus.RUNNING
    trace_id: str = ""
    resumed_from_burst_id: str | None = None  # the parent node — recorded at spawn
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    exit_code: int | None = None
    model: str | None = None  # requested at spawn, overwritten by the resolved model on harvest
    cost_usd: float | None = None  # advisory under subscription billing
    input_tokens: int | None = None
    output_tokens: int | None = None
    num_turns: int | None = None
    session_id: str | None = None
    session_key: str | None = None  # this burst's snapshot in the archive
    session_checksum: str | None = None  # its sha256; the ledger is the authority


# What `finish_burst` writes — everything a burst learns by ending. Shared by both
# backends on purpose: the CAS is the one statement they both hand-write, and the
# list drifting between them would be invisible until a field silently stopped
# being persisted on one of the two.
BURST_FINALIZE_COLUMNS = (
    "status",
    "finished_at",
    "exit_code",
    "model",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "num_turns",
    "session_id",
    "session_key",
    "session_checksum",
)


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
