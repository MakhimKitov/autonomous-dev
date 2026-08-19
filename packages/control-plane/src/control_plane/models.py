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


def utc_iso(value: str) -> str:
    """An ISO timestamp respelled in UTC — the spelling every stored stamp has.

    Every timestamp the ledger holds was written by `utc_now`, so it carries the
    `+00:00` offset. A caller's timestamp need not: `2026-08-17T12:00:00+06:00` is
    an earlier instant than `2026-08-17T09:00:00+00:00` but a later string. The
    Postgres backend compares instants and doesn't care; the SQLite one compares
    text, so anything it compares goes through here first (gate A7 measured the
    difference: `bursts_started_since` answered 0 where Postgres answered 2).
    """
    parsed = datetime.fromisoformat(value)
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def task_id_for(repository: str, issue_number: int) -> str:
    """A task's id, derived and never invented: ``{owner}/{repo}#{issue_number}``.

    Two properties come from deriving it (TO-BE §2, STORES.md). It is stable across
    replicas — two of them looking at the same issue compute the same id without
    coordinating — and it joins the GitHub world for free, so a row and a URL name
    the same thing. `UNIQUE (repository, issue_number)` then makes a second task
    for one issue unrepresentable rather than unlikely.
    """
    return f"{repository}#{issue_number}"


@dataclass(slots=True)
class TaskRecord:
    """One GitHub issue worked to completion, keyed by its derived id."""

    task_id: str  # '{owner}/{repo}#{issue_number}' — see task_id_for
    repository: str  # 'owner/name', the registered product; never a clone path
    issue_number: int
    status: str = TaskStatus.OPEN
    session_id: str | None = None
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
    # The burst span's own id. Both halves are here because a successor's span link
    # names a span, not a trace — see `telemetry.link_to_burst`. Empty when tracing
    # produced no span, which is a state and not a failure.
    span_id: str = ""
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
