"""The subscription pool — the platform runs on refs, never on tokens (TO-BE §4).

Every team member has a Claude subscription; the platform's compute is that
pooled capacity. A member opts in with `claude setup-token` (a one-year,
inference-only OAuth token), **materializes it themselves** — env var, file,
whatever the adopter's tooling writes — and registers **only a ref**. The
ledger holds `env:MAKSIM_CLAUDE_TOKEN`; the value lives in the member's
materialization, is resolved at spawn, enters one container and dies with it.

Three consequences the rest of the pool is built on:

- **Custody never moves.** Deleting the secret at its source revokes the
  contribution whatever the ledger says — a lever the platform cannot override.
- **Nothing can leak what nothing holds.** A ledger dump, a log line, an
  archived session: none of them can carry a token, because no table, no
  formatter and no serializer ever sees one. The wall is the absence, not a
  redaction pass — and it is enforced three deep: this module's grammar, both
  stores' verbs, and the schema's own CHECK.
- **Usage is derived.** Consumption is a rolling-window aggregate over `bursts`
  (§12: the ledger is the metrics source, never a mirrored counter), so the
  only pool state a row keeps beyond its declaration is `backoff_until`.

This module is the engine side — the grammar and the ceilings. The rows are the
stores'; the pick that spends them is admission's (F2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from secret_refs import SecretError, SecretResolver, parse_ref

from control_plane.models import (
    USAGE_WINDOW_H,
    SubscriptionRecord,
    SubscriptionStatus,
    utc_iso,
    utc_now,
)

log = structlog.get_logger()

# What `subscription set` may touch. `status` is deliberately absent — `revoke`
# owns it, with its reason discipline — and so is `backoff_until`, which is the
# attempt rail's (F3) and no operator's. The key and `registered_at` are nobody's.
SUBSCRIPTION_SET_COLUMNS: tuple[str, ...] = ("secret_ref", "cap_tokens", "expires_at")

# A member handle, normalized: the id is derived from it, so it has to be
# something a log line, a CLI argument and a `reporting.contribution` row can
# all carry without quoting.
_HANDLE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CapacityExhausted(RuntimeError):
    """The pool had nothing to give: every contributed subscription is spent for
    this window, backing off, expired, revoked — or its secret is gone from
    where the member put it, which is the member's own revocation lever. Not a
    failure of the task: nothing was attempted, so nothing failed."""


class SubscriptionError(RuntimeError):
    """A pool verb the platform must refuse: a ref that is really a value, a
    member registered twice, a cap that is not capacity, an expiry in the past,
    a no-op revoke, or `set` reaching for a column that is not an operator's."""


def subscription_id_for(member: str) -> str:
    """A subscription's id, derived from the member's handle and never invented
    (A6, the rule the task ids follow). One member, one contributed
    subscription: registering a second is a refusal that names the first, not a
    silent second row, and `subscription revoke maksim` is typeable by the
    person who ran `subscription register --member maksim`."""
    handle = (member or "").strip().lower().replace(" ", "-")
    if not _HANDLE.match(handle):
        raise SubscriptionError(
            f"a member handle is lowercase letters, digits, dot, dash or underscore — got {member!r}"
        )
    return handle


def check_ref(ref: str) -> str:
    """The ingestion wall, in the one place both backends pass through: what the
    ledger stores is a name. The resolver's own grammar decides (D4), so a token
    value — which has no `env:` / `file:` / `helper:` scheme — cannot be spelled
    as a ref by accident, and the error never echoes what it refused."""
    try:
        parse_ref(ref)
    except SecretError:
        # deliberately neither echoing the input nor chaining the cause: what was
        # rejected may *be* a token, and this message reaches a terminal and a log
        raise SubscriptionError(
            "the pool stores refs, never tokens — a ref is env:NAME, file:PATH or helper:NAME"
        ) from None
    return ref.strip()


def check_cap(cap_tokens: int) -> int:
    """The cap is tokens per rolling window — the member's own ceiling, and the
    number headroom is measured against (F2). Zero would register a
    contribution that can never be picked."""
    if int(cap_tokens) <= 0:
        raise SubscriptionError(f"a cap is tokens per window and must be positive, got {cap_tokens}")
    return int(cap_tokens)


def check_expiry(expires_at: str, *, now: str | None = None) -> str:
    """The token's own year, as the member declared it — a date or a full
    timestamp, stored in the one spelling the ledger keeps (UTC ISO). An expiry
    already past would register a subscription no pick could ever use;
    re-materializing at the same ref and moving this stamp is renewal (F4)."""
    try:
        stamp = utc_iso(expires_at)
    except ValueError as exc:
        raise SubscriptionError(f"an expiry is a date or an ISO timestamp, got {expires_at!r}") from exc
    if stamp <= (now or utc_now()):
        raise SubscriptionError(f"that expiry is already past: {stamp}")
    return stamp


def check_settable(changes: dict) -> None:
    if not changes:
        raise SubscriptionError("nothing to set — name at least one column to change")
    unknown = sorted(set(changes) - set(SUBSCRIPTION_SET_COLUMNS))
    if unknown:
        raise SubscriptionError(
            f"not settable: {', '.join(unknown)} — `subscription set` touches only "
            f"{', '.join(SUBSCRIPTION_SET_COLUMNS)}"
        )


def declare(member: str, ref: str, cap_tokens: int, expires_at: str) -> SubscriptionRecord:
    """The one place a registration is built, so every caller — CLI, console
    later, a test — gets the same checked row."""
    return SubscriptionRecord(
        subscription_id=subscription_id_for(member),
        member=member.strip(),
        secret_ref=check_ref(ref),
        cap_tokens=check_cap(cap_tokens),
        expires_at=check_expiry(expires_at),
        status=SubscriptionStatus.ACTIVE,
    )



# --- the pick (F2): who pays for the next burst ---


@dataclass(frozen=True, slots=True)
class Capacity:
    """Which contributed subscription pays for a burst, and the token that
    proves it. `MACHINE_CREDENTIAL` — both fields None — is an empty pool: the
    burst runs on whatever Anthropic credential this machine holds, which is the
    MVP's shape and still the single-owner laptop's. A burst records the id and
    hands the token to the box; neither the token nor this object is written
    anywhere."""

    subscription_id: str | None = None
    token: str | None = None
    headroom: int | None = None


MACHINE_CREDENTIAL = Capacity()


def window_start(now: str | None = None, window_h: float = USAGE_WINDOW_H) -> str:
    """The oldest burst the headroom question counts."""
    moment = datetime.fromisoformat(utc_iso(now or utc_now()))
    return (moment - timedelta(hours=window_h)).astimezone(UTC).isoformat()


def headroom(row: SubscriptionRecord, spent: int) -> int:
    """What is left of this member's window. Never negative in meaning — a
    member who overshot has no capacity, not negative capacity — but the
    subtraction is reported as it comes out, because the overshoot is worth
    seeing in a log line."""
    return row.cap_tokens - spent


def eligible(row: SubscriptionRecord, spent: int, now: str) -> bool:
    """The predicate, pure and whole (TO-BE §4): active ∧ unexpired ∧ not
    backing off ∧ headroom > 0. Kept in Python rather than in the pick's WHERE
    clause so there is one statement of it, testable without a database, and one
    aggregate for the store to answer."""
    return (
        row.status == SubscriptionStatus.ACTIVE
        and row.expires_at > now
        and (row.backoff_until is None or row.backoff_until <= now)
        and headroom(row, spent) > 0
    )


def candidates(
    rows: list[SubscriptionRecord], usage: dict[str, int], now: str | None = None
) -> list[SubscriptionRecord]:
    """The eligible subscriptions, most headroom first — the pick's order.

    Ties break on the id, so the answer is the same on every replica reading the
    same rows; a pool where two members have identical caps and no usage yet
    would otherwise depend on row order.
    """
    moment = now or utc_now()
    fit = [row for row in rows if eligible(row, usage.get(row.subscription_id, 0), moment)]
    return sorted(
        fit, key=lambda row: (-headroom(row, usage.get(row.subscription_id, 0)), row.subscription_id)
    )


def pick_capacity(
    ledger,
    resolver: SecretResolver | None = None,
    *,
    now: str | None = None,
    window_h: float = USAGE_WINDOW_H,
) -> Capacity:
    """Who pays for the next burst — the pool's half of admission (TO-BE §5).

    Two reads and no write: the rows, and one rolling-window aggregate over
    `bursts`. An empty pool is `MACHINE_CREDENTIAL`, so a deployment that has
    registered nobody behaves exactly as it did before F2. Otherwise the
    candidates are walked in headroom order and the first whose ref *resolves*
    pays: a ref that points at nothing is a subscription whose member deleted
    the secret at its source, which is their revocation lever and needs no verb
    to work — it is logged by id and skipped, never a crash and never a reason
    to fall back to the machine's own credential, because that would spend the
    operator's capacity in a member's name.

    Nothing eligible raises `CapacityExhausted`; the caller idles and says so
    (`capacity_exhausted`). Concurrency: two replicas can pick the same
    subscription in the same instant and both spend it. That is deliberate — a
    burst's token count is not known until it ends, so headroom is always a
    reading of *finished* work and the cap is a budget rail rather than a wall.
    The wall is the provider's own 429, which F3 turns into `backoff_until`.
    """
    rows = ledger.list_subscriptions()
    if not rows:
        return MACHINE_CREDENTIAL
    moment = now or utc_now()
    usage = ledger.subscription_usage(window_start(moment, window_h))
    resolver = resolver if resolver is not None else SecretResolver()
    unresolved: list[str] = []
    for row in candidates(rows, usage, moment):
        left = headroom(row, usage.get(row.subscription_id, 0))
        try:
            token = resolver.resolve(row.secret_ref)
        except SecretError as exc:
            # the message names the ref, never a value — SecretError is built that way
            log.warning(
                "a contributed subscription's secret is not there — skipping it",
                subscription=row.subscription_id, ref=row.secret_ref, detail=str(exc),
            )
            unresolved.append(row.subscription_id)
            continue
        log.info("burst pays from the pool", subscription=row.subscription_id, headroom=left)
        return Capacity(subscription_id=row.subscription_id, token=token, headroom=left)
    raise CapacityExhausted(
        f"no eligible subscription: {len(rows)} contributed, "
        f"{len(unresolved)} with a secret that is no longer there"
    )
