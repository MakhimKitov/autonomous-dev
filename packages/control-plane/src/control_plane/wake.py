"""Wakes (E5): webhooks as wake-up, polls as truth (TO-BE §5).

The receiver — the platform's only public surface, a separate package that
never imports this one — verifies a GitHub delivery and forwards a *wake*
(``repository``, ``kind``, ``delivery``) to the internal endpoint this module
serves. The endpoint does one ledger write, `wake_repository`: the product's
observation lease is cleared and a `product_woken` event announced, in one
statement, and the replicas' wake channel is notified. Then it nudges its own
loop. Every replica's loop sleeps on its `wake` flag rather than on a plain
timer, and the Postgres ledger's `subscribe_wakes` sets that flag from the
channel — so a webhook that reached any one replica has every replica looking
within a second, and the observation lease (E2) still makes exactly one of them
poll GitHub.

The endpoint is internal and unauthenticated, by design: the worst a caller
can do is make the platform poll real GitHub state once — the same price as a
forged webhook, and the reason the public process needs no trust anywhere
(TO-BE §5, "forgery is harmless by construction"). It must not be routed from
the outside; the reference manifests put it on the cluster network only.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import structlog

from control_plane.store import Ledger

log = structlog.get_logger()

WAKE_BIND_ENV = "WAKE_BIND"  # host:port for the internal endpoint; unset ⇒ none (polls only)


def parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"{WAKE_BIND_ENV} is host:port (got {bind!r})")
    return host or "0.0.0.0", int(port)


class WakeEndpoint:
    """``POST /wake`` → `wake_repository` → the loop's flag; ``GET /healthz`` → ok.

    A daemon thread around the stdlib server: it lives as long as the process,
    and the loop never waits on it. ``ledger`` must be the endpoint's own handle
    — this thread writes through it while the loop writes through the loop's;
    a single-node connection is thread-bound, and even a service one must not
    have two threads' statements interleave. ``wake`` is the engine's flag
    (`_serve` sleeps on it)."""

    def __init__(self, bind: str, ledger: Ledger, wake: threading.Event) -> None:
        host, port = parse_bind(bind)
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server's shape
                if self.path != "/wake":
                    self._reply(404, {"error": "POST /wake is the one verb here"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) if length else b"")
                    repository, kind = body["repository"], body.get("kind", "")
                    delivery = body.get("delivery") or None
                    if not isinstance(repository, str) or "/" not in repository:
                        raise ValueError("repository is owner/name")
                except (ValueError, KeyError, TypeError) as exc:
                    self._reply(400, {"error": f"a wake is JSON with repository, kind, delivery ({exc})"})
                    return
                if endpoint.wake_repository(repository, str(kind), delivery):
                    self._reply(202, {"woken": True})
                else:
                    self._reply(404, {"woken": False, "error": f"{repository} is not an active product"})

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    self._reply(200, {"ok": True})
                else:
                    self._reply(404, {"error": "GET /healthz, POST /wake"})

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args) -> None:  # noqa: A002 — http.server's signature
                pass  # the endpoint logs the wake itself, with the delivery id

        self._ledger = ledger
        self._wake = wake
        self._lock = threading.Lock()  # one wake write at a time: the server is a thread per request
        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="wake-endpoint", daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> WakeEndpoint:
        self._thread.start()
        log.info("wake endpoint listening", host=self._server.server_address[0], port=self.port)
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wake_repository(self, repository: str, kind: str, delivery: str | None) -> bool:
        """The one write, then the nudge. A product that is not active is
        reported, not woken — and the loop is not nudged for it, because nothing
        about it would be observed."""
        try:
            with self._lock:
                woken = self._ledger.wake_repository(repository, kind, delivery)
        except Exception as exc:  # noqa: BLE001 — any store error; the wake is best-effort, the poll is the truth
            log.warning("wake not recorded — the poll catches up", repo=repository, error=str(exc))
            return False
        if woken:
            log.info("product woken", repo=repository, kind=kind, delivery=delivery)
            self._wake.set()
        else:
            log.info("wake for a product that is not active — ignored", repo=repository, kind=kind, delivery=delivery)
        return woken


def attach_wakes(engine: Any, ledger: Ledger, bind: str | None = None) -> WakeEndpoint | None:
    """Wire an engine (a `Fleet` or a `Scheduler`, anything with a ``wake`` flag)
    to both wake sources: the ledger's channel — a peer's endpoint took a webhook
    — and, when ``bind`` is given (``WAKE_BIND``), this process's own endpoint.
    ``ledger`` is a handle opened for this — `open_ledger` a second time — never
    the loop's own (see `WakeEndpoint`). Called once, before `_serve`; the
    returned endpoint is for tests to close."""

    def on_wake(repository: str) -> None:
        log.info("wake from a peer — looking now", repo=repository)
        engine.wake.set()

    ledger.subscribe_wakes(on_wake)
    bind = (bind if bind is not None else os.environ.get(WAKE_BIND_ENV, "")).strip()
    if not bind:
        return None
    return WakeEndpoint(bind, ledger, engine.wake).start()
