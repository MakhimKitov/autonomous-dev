"""`webhook-receiver serve` — the HTTP shell around `Receiver`.

Configuration is three variables, all read at startup:

- ``WEBHOOK_APPS`` — ``<app id>=<secret ref>[,…]``: the GitHub Apps whose
  webhooks point here and the ref (``env:`` / ``file:`` / ``helper:``) of each
  one's webhook secret. Resolved once, here; an unresolvable ref refuses to start.
- ``WAKE_URL`` — the control plane's internal wake endpoint (``WAKE_BIND`` on
  its side), e.g. ``http://control-plane:8081``.
- ``RECEIVER_BIND`` — ``host:port`` to listen on (default ``0.0.0.0:8080``).
  Port 0 binds an ephemeral one; the ``listening`` log line names it.

GitHub POSTs deliveries to any path; ``GET /healthz`` answers ``ok`` for the
platform in front of this process. Logs are logfmt on stdout, one line per
delivery, keyed by the delivery id — the same id the control plane stores on
the `product_woken` event, so the two logs and the ledger line up.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import structlog
from secret_refs import SecretError

from webhook_receiver.receiver import Receiver, forward, parse_apps

log = structlog.get_logger()

APPS_ENV = "WEBHOOK_APPS"
WAKE_URL_ENV = "WAKE_URL"
BIND_ENV = "RECEIVER_BIND"
DEFAULT_BIND = "0.0.0.0:8080"


def configure_logging() -> None:
    # dev_agent.log's shape, restated: this package imports neither the control
    # plane nor the agent (TO-BE §5 — nothing but the HMAC filter behind the
    # public route), so the ten lines are cheaper than the dependency
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.LogfmtRenderer(key_order=["timestamp", "level", "event"]),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"{BIND_ENV} is host:port (got {bind!r})")
    return host or "0.0.0.0", int(port)


class Handler(BaseHTTPRequestHandler):
    receiver: Receiver  # set on the class the server is built with

    def do_POST(self) -> None:  # noqa: N802 — http.server's shape
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        verdict = self.receiver.handle(dict(self.headers.items()), body)
        self._reply(verdict.status, {"outcome": verdict.outcome, "detail": verdict.detail})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"error": "deliveries are POSTed; GET /healthz is the only read"})

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — http.server's signature
        pass  # the receiver logs each delivery itself, with its id


def build_server(bind: str, receiver: Receiver) -> ThreadingHTTPServer:
    host, port = parse_bind(bind)
    handler = type("BoundHandler", (Handler,), {"receiver": receiver})
    return ThreadingHTTPServer((host, port), handler)


def serve(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="webhook-receiver")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve"])
    parser.parse_args(argv)
    configure_logging()
    spec, wake_url = os.environ.get(APPS_ENV, ""), os.environ.get(WAKE_URL_ENV, "").strip()
    if not wake_url:
        print(f"error: {WAKE_URL_ENV} is unset — where would a wake go?", file=sys.stderr)
        return 2
    try:
        apps = parse_apps(spec)
    except (ValueError, SecretError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    receiver = Receiver(apps, functools.partial(forward, wake_url))
    try:
        server = build_server(os.environ.get(BIND_ENV, DEFAULT_BIND), receiver)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    log.info("listening", host=host, port=port, apps=sorted(apps), wake_url=wake_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    return serve(argv)


if __name__ == "__main__":
    sys.exit(main())
