"""Verify, dedup, forward — as functions and one small object, HTTP-free.

`Receiver.handle` takes the delivery's headers and raw body and answers with a
`Verdict`: the HTTP status to give GitHub and the one-word outcome the log line
and the tests key on. The HTTP server (`cli.py`) is a thin shell around it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import structlog
from secret_refs import SecretResolver

log = structlog.get_logger()

# The headers GitHub stamps on every App webhook delivery.
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"
SIGNATURE_HEADER = "X-Hub-Signature-256"
APP_HEADER = "X-GitHub-Hook-Installation-Target-ID"

# The events that are a reason to look at a product again (TO-BE §5). The event
# name travels as the wake's `kind`, unchanged: the control plane logs it and
# stores it, and decides nothing on it — the poll decides.
WAKE_EVENTS = frozenset({"issues", "pull_request", "pull_request_review", "push"})

# Delivery ids remembered. GitHub redelivers on its own for hours at most, and a
# replay older than the newest few thousand deliveries costs one poll, not a
# breach — so this is a size, not a knob.
DEDUP_CAPACITY = 4096

FORWARD_TIMEOUT_S = 3.0


@dataclass(frozen=True, slots=True)
class Wake:
    repository: str
    kind: str
    delivery: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """What GitHub is told, and what the log says. ``outcome`` is one of
    ``forwarded`` · ``undelivered`` (a wake the control plane did not take —
    GitHub still gets its 202) · ``replayed`` · ``forged`` · ``ignored`` ·
    ``malformed``."""

    status: int
    outcome: str
    detail: str = ""


def parse_apps(spec: str, resolver: SecretResolver | None = None) -> dict[str, str]:
    """``WEBHOOK_APPS`` → {app id: webhook secret}. ``"<id>=<ref>,<id>=<ref>"``,
    each ref a `secret_refs` ref (``env:NAME`` / ``file:PATH`` / ``helper:NAME``),
    resolved here, once, at startup: a ref that points at nothing refuses to
    start rather than refusing every delivery later. The ids are the Apps'
    (`X-GitHub-Hook-Installation-Target-ID`), one entry per App whose webhook
    points here."""
    resolver = resolver or SecretResolver()
    apps: dict[str, str] = {}
    for entry in (part.strip() for part in spec.split(",")):
        if not entry:
            continue
        app_id, sep, ref = entry.partition("=")
        app_id = app_id.strip()
        if not sep or not app_id.isdigit() or not ref.strip():
            raise ValueError(
                f"WEBHOOK_APPS entries are <app id>=<secret ref>, e.g. 123456=env:DEV_WEBHOOK_SECRET (got {entry!r})"
            )
        apps[app_id] = resolver.resolve(ref.strip())
    if not apps:
        raise ValueError("WEBHOOK_APPS names no App — nothing could ever verify")
    return apps


def verify(secret: str, body: bytes, signature: str | None) -> bool:
    """GitHub's `X-Hub-Signature-256`: ``sha256=<hex hmac of the raw body>``.
    Constant-time compare; a missing or malformed header is simply false."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[len("sha256="):].strip())


class Dedup:
    """The delivery ids seen, newest-last, capped. In memory on purpose: a
    restart forgets everything and the price is one duplicate poll per replay
    (TO-BE §5) — state here would be a credential to protect and a store to run."""

    def __init__(self, capacity: int = DEDUP_CAPACITY) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def seen_before(self, delivery: str) -> bool:
        """Records ``delivery``; True if it was already here."""
        if delivery in self._seen:
            self._seen.move_to_end(delivery)
            return True
        self._seen[delivery] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False


def wake_of(event: str, payload: Mapping) -> Wake | None:
    """The wake a delivery is, or None when it is nothing to poll for — an event
    the platform does not act on, or one that names no repository."""
    if event not in WAKE_EVENTS:
        return None
    repository = (payload.get("repository") or {}).get("full_name")
    if not repository:
        return None
    return Wake(repository=repository, kind=event, delivery="")


def forward(wake_url: str, wake: Wake, timeout: float = FORWARD_TIMEOUT_S) -> tuple[bool, str]:
    """POST the wake to the control plane's internal endpoint. Fire-and-forget
    in spirit: a failure is logged by the caller and GitHub still gets its 2xx —
    the control plane's poll catches up on whatever a lost wake would have said."""
    body = json.dumps({"repository": wake.repository, "kind": wake.kind, "delivery": wake.delivery}).encode()
    request = urllib.request.Request(
        wake_url.rstrip("/") + "/wake", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"{response.status}"
    except urllib.error.HTTPError as exc:
        # the control plane answered, and said no — an unregistered or paused
        # product, most likely; nothing to retry
        return False, f"{exc.code} {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))


class Receiver:
    """One delivery in, one verdict out. ``forwarder`` is `forward` bound to the
    control plane's URL; tests hand in a recorder."""

    def __init__(self, apps: Mapping[str, str], forwarder: Callable[[Wake], tuple[bool, str]],
                 dedup: Dedup | None = None) -> None:
        self.apps = dict(apps)
        self.forwarder = forwarder
        self.dedup = dedup or Dedup()

    def handle(self, headers: Mapping[str, str], body: bytes) -> Verdict:
        get = _header_getter(headers)
        delivery, event, app_id = get(DELIVERY_HEADER), get(EVENT_HEADER), get(APP_HEADER)
        if not delivery or not event:
            return Verdict(400, "malformed", "missing delivery or event header")
        # verify first — nothing below is trusted until this passes, not even
        # the delivery id, so a forger cannot poison the dedup either
        secret = self.apps.get(app_id or "")
        if secret is None:
            log.warning("delivery forged — unknown App", delivery=delivery, app=app_id or "?")
            return Verdict(401, "forged", "unknown App")
        if not verify(secret, body, get(SIGNATURE_HEADER)):
            log.warning("delivery forged — bad signature", delivery=delivery, app=app_id)
            return Verdict(401, "forged", "bad signature")
        if self.dedup.seen_before(delivery):
            log.info("delivery replayed — dropped", delivery=delivery, app=app_id)
            return Verdict(200, "replayed")
        if event == "ping":
            log.info("ping", delivery=delivery, app=app_id)
            return Verdict(200, "ignored", "pong")
        try:
            payload = json.loads(body)
        except ValueError:
            return Verdict(400, "malformed", "body is not JSON")
        wake = wake_of(event, payload if isinstance(payload, dict) else {})
        if wake is None:
            log.info("delivery ignored — not a wake", delivery=delivery, github_event=event)
            return Verdict(200, "ignored", f"{event} is not a wake")
        wake = Wake(wake.repository, wake.kind, delivery)
        ok, detail = self.forwarder(wake)
        if ok:
            log.info("delivery forwarded", delivery=delivery, repo=wake.repository, kind=wake.kind, app=app_id)
            return Verdict(202, "forwarded", detail)
        # GitHub still gets its 2xx: the wake is best-effort, the poll is the truth
        log.warning("wake not delivered — the poll catches up", delivery=delivery,
                    repo=wake.repository, kind=wake.kind, error=detail)
        return Verdict(202, "undelivered", detail)


def _header_getter(headers: Mapping[str, str]) -> Callable[[str], str | None]:
    lowered = {key.lower(): value for key, value in headers.items()}
    return lambda name: lowered.get(name.lower())
