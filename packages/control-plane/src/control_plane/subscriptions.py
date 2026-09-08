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

from secret_refs import SecretError, parse_ref

from control_plane.models import SubscriptionRecord, SubscriptionStatus, utc_iso, utc_now

# What `subscription set` may touch. `status` is deliberately absent — `revoke`
# owns it, with its reason discipline — and so is `backoff_until`, which is the
# attempt rail's (F3) and no operator's. The key and `registered_at` are nobody's.
SUBSCRIPTION_SET_COLUMNS: tuple[str, ...] = ("secret_ref", "cap_tokens", "expires_at")

# A member handle, normalized: the id is derived from it, so it has to be
# something a log line, a CLI argument and a `reporting.contribution` row can
# all carry without quoting.
_HANDLE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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
