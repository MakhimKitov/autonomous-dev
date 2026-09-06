"""The webhook receiver (E5): verify, dedup, forward — and nothing else in the box.

Each function alone, then `Receiver.handle` with a recording forwarder, then the
HTTP shell on an ephemeral port. The whole path — a signed delivery reaching a
replica's poll through the control plane's endpoint — is `test_wakes.py`.
"""

import hashlib
import hmac
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from secret_refs import SecretError, SecretResolver
from webhook_receiver import cli
from webhook_receiver.receiver import (
    APP_HEADER,
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    Dedup,
    Receiver,
    Wake,
    forward,
    parse_apps,
    verify,
    wake_of,
)

SECRET = "s3cret"
APP = "123456"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def delivery(event="issues", repo="owner/product", delivery_id="d-1", secret=SECRET, app=APP, body=None):
    """A GitHub App webhook delivery: headers + raw body, signed."""
    raw = body if body is not None else json.dumps({"action": "labeled", "repository": {"full_name": repo}}).encode()
    headers = {DELIVERY_HEADER: delivery_id, EVENT_HEADER: event, APP_HEADER: app, SIGNATURE_HEADER: sign(raw, secret)}
    return headers, raw


class RecordingForwarder:
    def __init__(self, answer=(True, "202")):
        self.wakes: list[Wake] = []
        self.answer = answer

    def __call__(self, wake):
        self.wakes.append(wake)
        return self.answer


@pytest.fixture
def receiver():
    forwarder = RecordingForwarder()
    return Receiver({APP: SECRET}, forwarder), forwarder


# --- the parts ---


def test_apps_are_ids_and_secret_refs_resolved_once():
    resolver = SecretResolver({"DEV_WH": "one", "REV_WH": "two"})
    assert parse_apps("111=env:DEV_WH, 222=env:REV_WH", resolver) == {"111": "one", "222": "two"}
    with pytest.raises(ValueError, match="<app id>=<secret ref>"):
        parse_apps("111", resolver)
    with pytest.raises(ValueError, match="<app id>=<secret ref>"):
        parse_apps("dev=env:DEV_WH", resolver)  # the id is the App's number
    with pytest.raises(ValueError, match="names no App"):
        parse_apps("", resolver)
    with pytest.raises(SecretError):
        parse_apps("111=env:MISSING", resolver)  # refuses at startup, not per delivery
    with pytest.raises(SecretError):
        parse_apps("111=hunter2", resolver)  # a value is not a ref


def test_the_signature_is_the_hmac_of_the_raw_body():
    body = b'{"a": 1}'
    assert verify(SECRET, body, sign(body)) is True
    assert verify(SECRET, body, sign(body, "other")) is False
    assert verify(SECRET, b'{"a":1}', sign(body)) is False  # the bytes, not the JSON
    assert verify(SECRET, body, None) is False
    assert verify(SECRET, body, "sha1=" + "0" * 40) is False


def test_dedup_remembers_the_newest_ids_only():
    dedup = Dedup(capacity=3)
    assert [dedup.seen_before(d) for d in ("a", "b", "c")] == [False, False, False]
    assert dedup.seen_before("a") is True
    dedup.seen_before("d")  # capacity 3: the least recently seen goes — that is b (a was just touched)
    assert dedup.seen_before("b") is False
    assert dedup.seen_before("a") is True


def test_a_wake_is_a_platform_event_naming_a_repository():
    payload = {"repository": {"full_name": "owner/product"}}
    assert wake_of("issues", payload) == Wake("owner/product", "issues", "")
    assert wake_of("pull_request_review", payload).kind == "pull_request_review"
    assert wake_of("installation", payload) is None  # not something the poll acts on
    assert wake_of("issues", {"repository": {}}) is None
    assert wake_of("issues", {}) is None


# --- the verdicts ---


def test_a_signed_delivery_is_forwarded_with_its_id(receiver):
    rec, forwarder = receiver
    verdict = rec.handle(*delivery())
    assert (verdict.status, verdict.outcome) == (202, "forwarded")
    assert forwarder.wakes == [Wake("owner/product", "issues", "d-1")]


def test_a_replayed_delivery_is_dropped(receiver):
    rec, forwarder = receiver
    rec.handle(*delivery())
    verdict = rec.handle(*delivery())  # the same id again — GitHub's redelivery, or a captured replay
    assert (verdict.status, verdict.outcome) == (200, "replayed")
    assert len(forwarder.wakes) == 1


def test_a_forged_delivery_is_refused_and_poisons_nothing(receiver):
    """Bad signature or unknown App: 401, no forward — and the forged id is not
    remembered, so the real delivery with that id still gets through."""
    rec, forwarder = receiver
    forged = rec.handle(*delivery(secret="guess"))
    assert (forged.status, forged.outcome, forged.detail) == (401, "forged", "bad signature")
    unknown = rec.handle(*delivery(app="999"))
    assert (unknown.status, unknown.outcome, unknown.detail) == (401, "forged", "unknown App")
    assert forwarder.wakes == []
    assert rec.handle(*delivery()).outcome == "forwarded"  # same id as the forgeries: not a replay


def test_what_is_not_a_wake_is_acknowledged_and_dropped(receiver):
    rec, forwarder = receiver
    assert rec.handle(*delivery(event="ping", body=b'{"zen": "hi"}')).outcome == "ignored"
    assert rec.handle(*delivery(event="installation", delivery_id="d-2")).outcome == "ignored"
    assert forwarder.wakes == []


def test_malformed_deliveries_are_400(receiver):
    rec, _ = receiver
    headers, body = delivery()
    del headers[DELIVERY_HEADER]
    assert rec.handle(headers, body).status == 400
    headers, _ = delivery(body=b"not json")
    assert rec.handle(headers, b"not json").outcome == "malformed"


def test_a_wake_the_control_plane_did_not_take_is_still_a_202():
    """The forward failed — control plane down, or it said 404 — and GitHub
    still gets its 2xx: the wake is best-effort, the poll is the truth."""
    forwarder = RecordingForwarder(answer=(False, "404 Not Found"))
    rec = Receiver({APP: SECRET}, forwarder)
    verdict = rec.handle(*delivery())
    assert (verdict.status, verdict.outcome, verdict.detail) == (202, "undelivered", "404 Not Found")


def test_headers_are_matched_case_insensitively(receiver):
    rec, forwarder = receiver
    headers, body = delivery()
    lowered = {k.lower(): v for k, v in headers.items()}  # what an http.server hands over
    assert rec.handle(lowered, body).outcome == "forwarded"


# --- the forward, against a control plane stand-in ---


class FakeControlPlane(HTTPServer):
    def __init__(self, status=202):
        self.received = []
        self.status = status
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                fake.received.append((self.path, json.loads(self.rfile.read(length))))
                body = b'{"woken": true}' if fake.status < 300 else b'{"woken": false}'
                self.send_response(fake.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


def test_forward_posts_the_wake_and_reports_the_answer():
    cp = FakeControlPlane()
    try:
        assert forward(cp.url, Wake("owner/product", "push", "d-7")) == (True, "202")
        assert cp.received == [("/wake", {"repository": "owner/product", "kind": "push", "delivery": "d-7"})]
    finally:
        cp.shutdown()
    refused = FakeControlPlane(status=404)
    try:
        ok, detail = forward(refused.url, Wake("owner/product", "push", "d-8"))
        assert ok is False and detail.startswith("404")
    finally:
        refused.shutdown()
    ok, _ = forward("http://127.0.0.1:1", Wake("owner/product", "push", "d-9"), timeout=1)  # nobody there
    assert ok is False


# --- the HTTP shell ---


def test_the_server_answers_deliveries_and_health():
    forwarder = RecordingForwarder()
    server = cli.build_server("127.0.0.1:0", Receiver({APP: SECRET}, forwarder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        headers, body = delivery()
        with urllib.request.urlopen(urllib.request.Request(base + "/", data=body, headers=headers), timeout=5) as r:
            assert r.status == 202 and json.loads(r.read())["outcome"] == "forwarded"
        assert forwarder.wakes[0].delivery == "d-1"
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(urllib.request.Request(base + "/", data=body, headers={**headers, SIGNATURE_HEADER: "sha256=00"}), timeout=5)
        assert refused.value.code == 401
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            assert r.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_serve_refuses_to_start_without_its_three_answers(monkeypatch, capsys):
    monkeypatch.delenv(cli.WAKE_URL_ENV, raising=False)
    assert cli.serve([]) == 2
    assert cli.WAKE_URL_ENV in capsys.readouterr().err
    monkeypatch.setenv(cli.WAKE_URL_ENV, "http://control-plane:8081")
    monkeypatch.setenv(cli.APPS_ENV, "123=env:NOT_SET_ANYWHERE")
    assert cli.serve([]) == 2
    assert "NOT_SET_ANYWHERE" in capsys.readouterr().err


# --- the wall: credential-free by construction ---


def test_the_receiver_loads_no_platform_code_and_no_store_client():
    """TO-BE §5: the public process is an HMAC filter and nothing else — no DB
    client, no GitHub client, no control-plane module can be behind the route.
    Checked in a fresh interpreter, on what importing the receiver actually loads."""
    probe = (
        "import sys, webhook_receiver.cli, webhook_receiver.receiver;"
        "banned = ('control_plane', 'dev_agent', 'psycopg', 'boto3', 'botocore', 'jwt', 'requests');"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in banned);"
        "assert not loaded, loaded"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
