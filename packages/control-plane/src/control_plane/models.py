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


class ProductState(StrEnum):
    """A registered product is scheduled or it is not — nothing in between."""

    ACTIVE = "active"
    PAUSED = "paused"


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
class RepositoryRecord:
    """One registered product — the tenancy unit (TO-BE §2).

    Every policy and quota column holds a materialized value, never a
    NULL-that-means-inherit: `product register` fills them from platform
    defaults unless overridden, and after that the row is the entire truth
    about the product. A changed platform default never retro-propagates —
    changing a live product is `product set`, an audited operator action.
    """

    repository: str  # 'owner/name' — the primary key, same spelling tasks use
    team: str  # owning team: gate authority, where escalations go
    state: str = ProductState.ACTIVE
    review_enabled: bool = False
    review_provider: str = ""  # the selector, spelled as REVIEW_PROVIDERS spells it
    e2e_enabled: bool = False
    test_env_refs: dict = field(default_factory=dict)  # name → secret ref, never a value
    concurrency: int = 1
    daily_bursts: int = 20  # mirrors Rails.daily_burst_ceiling
    usage_share: float = 1.0
    last_burst_at: str | None = None  # least-recently-served scheduling (C4)
    claimed_by: str | None = None  # observation lease (E2)
    claimed_until: str | None = None
    registered_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class RoleRecord:
    """One role row — a declarative execution contract (TO-BE §4, D5).

    Ceilings in code, composition in data: the wall-bearing fields (`role`,
    `kind`, `identity`, `sink`, `tools`) of a pipeline row are frozen by the
    store; the open ones (`prompt_preamble`, `model`, `max_budget_usd`,
    `max_turns`, `mount_mode`) are `role set`'s, audited. `execute_burst`
    reads the row at every spawn, so an edit governs the next burst.
    """

    role: str  # the primary key; pipeline identity is code keyed on this name
    kind: str  # 'pipeline' | 'custom'
    prompt_preamble: str = ""  # composed *after* the image's role preamble, never instead
    tools: list = field(default_factory=lambda: ["*"])  # ['*'] or ⊆ roles.TOOL_ALLOWLIST
    model: str = "claude-sonnet-5"
    max_budget_usd: float = 2.0
    max_turns: int = 100
    mount_mode: str = "rw"  # 'rw' | 'ro' — a row can narrow the mount, never widen it
    identity: str = "none"  # 'none' | 'automation' — the pipeline Apps are not values
    sink: str = "issue_comment"  # 'issue_comment' | 'ledger_artifact' | 'event'
    trigger: dict | None = None  # schedule/event — machinery lands at T4
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class AuditRecord:
    """One operator mutation, as the ledger recorded it — the read shape of the
    `audit` table (append-only by grant; TO-BE §2, "The audit log starts here")."""

    id: int
    actor: str
    action: str  # 'product.register' | 'product.pause' | 'product.resume' | 'product.set' | 'role.set' | 'org.stop' | 'org.resume'
    target: str  # the product slug, the role name, or ORG_TARGET
    reason: str
    at: str


# The audit target of the org verbs (E3): the org has no name of its own in the
# ledger, so its rows carry this one — `audit_trail(ORG_TARGET)` is its history.
ORG_TARGET = "org"


@dataclass(slots=True)
class OrgStop:
    """The org STOP as the ledger holds it (E3): the one `org_state` row while
    its `stop_reason` is set. `Ledger.org_stop` returns None for a running org —
    there is no "running" record, because the row carries nothing then."""

    reason: str
    stopped_by: str
    stopped_at: str


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
    # The lease (STORES.md, "Claim — a lease is data", E1): which replica holds
    # the task's next burst and until when. Stamped by `claim_next`, renewed by
    # the heartbeat while the container runs, cleared by the transition that
    # ends the burst. A lapsed lease is what a dead replica leaves behind —
    # visible data any peer may reclaim, never a held lock.
    claimed_by: str | None = None
    claimed_until: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


# What a state transition may write (E1). `task_id` is the key, `created_at` is
# history, and both backends check a transition's columns against this one tuple
# so a typo in the scheduler fails loudly instead of binding nothing.
TASK_TRANSITION_COLUMNS = (
    "status",
    "session_id",
    "pr_number",
    "attempts",
    "review_rounds",
    "next_burst_kind",
    "last_review_id",
    "last_burst_at",
    "claimed_by",
    "claimed_until",
)


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
# (TO-BE §12: any new violations row is a page — the three forms an adopter can
# consume it in are OBSERVABILITY.md, "The alert condition").
EVENT_VIOLATION_RECORDED = "violation_recorded"

# The registry's announcements (C1): one kind per operator verb, written in the
# same statement as the mutation and its audit row. The payload names the audit
# row (`audit_id`) so a consumer can read the who/why without a second contract.
EVENT_PRODUCT_REGISTERED = "product_registered"
EVENT_PRODUCT_PAUSED = "product_paused"
EVENT_PRODUCT_RESUMED = "product_resumed"
EVENT_PRODUCT_UPDATED = "product_updated"
EVENT_ROLE_UPDATED = "role_updated"  # `role set` (D5): payload names the role and the changed columns
# The org STOP (E3): `stop` and `resume`, same shape — the mutation, its audit row
# and this announcement in one statement. `org_stopped` also carries the reason,
# so the page it becomes needs no second read.
EVENT_ORG_STOPPED = "org_stopped"
EVENT_ORG_RESUMED = "org_resumed"

# The optional wake channel. A consumer may LISTEN on it to poll sooner; nothing
# is allowed to depend on a notification arriving.
EVENT_CHANNEL = "events"
