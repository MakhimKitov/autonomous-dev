"""E5's done-when (#36): forged and replayed deliveries produce at most one
extra poll, verified in logs — on the whole path, as processes.

Three processes over one Postgres: two `replica_harness.py` replicas (only the
first binds a wake endpoint — the second learns of wakes the way a peer does,
through the ledger's channel) and a real `webhook-receiver serve`, pointed at
the first replica's endpoint. The test plays GitHub: it signs deliveries with
the App's webhook secret and POSTs them to the receiver.

The replicas' poll interval is long, so a poll that happens is a poll the test
caused. A signed delivery has to produce a poll within seconds — on *some*
replica, and only one of them, the observation lease (E2) still standing
between the two — and a `product_woken` event with the delivery's id. Then the
two attacks TO-BE §5 says are harmless by construction: the same delivery
again (a replay), and the same body under a wrong signature and under an
unknown App (forgeries) — no poll, no event, the receiver's log naming each
outcome. And a second real delivery: one more poll, exactly.

Postgres only, like `test_replicas.py`: the stores CI job runs it, `LEDGER_DSN`
unset skips it.
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.models import WAKE_SETTLE_S, RepositoryRecord
from control_plane.postgres import PostgresLedger
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo
from test_replicas import HARNESS, Replica, hermetic_env, wait_for

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset")

REPOSITORY = "owner/product"
APP_ID = "424242"
SECRET = "drill-webhook-secret"
INTERVAL = 30  # the loop's sleep and the observation lease: far longer than the test, so polls are ours
PROMPT_S = WAKE_SETTLE_S + 3.0  # how soon after a wake the poll must have happened: the settle (E6), then a tick


@pytest.fixture()
def fresh_dsn():
    name = f"wakes_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    ledger = PostgresLedger(dsn)
    ledger.register_repository(RepositoryRecord(repository=REPOSITORY, team="owners"), actor="test")
    ledger.close()
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


class QuietReplica(Replica):
    """A harness replica with nothing to do but observe — no issues, a long
    interval — and, when asked, a wake endpoint on an ephemeral port."""

    def __init__(self, name: str, root: Path, dsn: str, wake_endpoint: bool):
        self.name = name
        self.data_dir = root / name
        self.data_dir.mkdir()
        self.events_path = root / f"{name}.events"
        self.stderr_path = root / f"{name}.stderr"
        self._stderr = self.stderr_path.open("w")
        argv = [
            sys.executable, str(HARNESS),
            "--dsn", dsn, "--data-dir", str(self.data_dir), "--events", str(self.events_path),
            "--issues", "0", "--interval", str(INTERVAL), "--poll-interval-s", str(INTERVAL),
        ]
        if wake_endpoint:
            argv += ["--wake-bind", "127.0.0.1:0"]
        # stdout too: the replica's log (logfmt) is where "product woken" is said
        self.proc = subprocess.Popen(argv, stdout=self._stderr, stderr=subprocess.STDOUT, env=hermetic_env())

    @property
    def wake_port(self) -> int | None:
        return self.events("up")[0]["wake_port"]

    def observes(self) -> list[float]:
        return [e["t"] for e in self.events("observe")]


class ReceiverProcess:
    """`webhook-receiver serve`, the real one, on an ephemeral port."""

    def __init__(self, root: Path, wake_url: str):
        self.log_path = root / "receiver.log"
        self._log = self.log_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; from webhook_receiver.cli import main; sys.exit(main(sys.argv[1:]))", "serve"],
            stdout=self._log, stderr=subprocess.STDOUT,
            env=hermetic_env(
                WEBHOOK_APPS=f"{APP_ID}=env:DRILL_WEBHOOK_SECRET", DRILL_WEBHOOK_SECRET=SECRET,
                WAKE_URL=wake_url, RECEIVER_BIND="127.0.0.1:0",
            ),
        )
        line = wait_for(lambda: next((ln for ln in self.log().splitlines() if "event=listening" in ln), None),
                        timeout=20, what=lambda: f"the receiver to listen\n{self.log()}")
        self.port = int(line.split("port=")[1].split()[0])

    def log(self) -> str:
        return self.log_path.read_text() if self.log_path.exists() else ""

    def deliver(self, headers: dict, body: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        finally:
            self._log.close()


def github_delivery(delivery_id: str, event: str = "issues", secret: str = SECRET, app: str = APP_ID):
    """What GitHub sends an App's webhook: the raw JSON body and its signature."""
    body = json.dumps({"action": "labeled", "repository": {"full_name": REPOSITORY}}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "X-GitHub-Hook-Installation-Target-ID": app,
        "X-Hub-Signature-256": signature,
    }
    return headers, body


def test_a_signed_delivery_is_one_poll_and_forgeries_and_replays_are_none(fresh_dsn, tmp_path):
    a = QuietReplica("a", tmp_path, fresh_dsn, wake_endpoint=True)
    b = QuietReplica("b", tmp_path, fresh_dsn, wake_endpoint=False)
    ledger = PostgresLedger(fresh_dsn)
    receiver = None

    def polls() -> list[float]:
        return sorted(a.observes() + b.observes())

    def woken_events():
        return [e for e in ledger.events() if e.kind == "product_woken"]

    try:
        wait_for(lambda: a.events("up") and b.events("up"), timeout=30, what=lambda: f"both replicas up\n{a.dump()}\n{b.dump()}")
        assert a.wake_port and b.wake_port is None
        # the startup poll: whichever replica's lease landed observed once; then
        # nothing happens for INTERVAL seconds unless someone wakes the fleet
        wait_for(lambda: len(polls()) == 1, timeout=15, what=lambda: f"the startup poll\n{a.dump()}\n{b.dump()}")
        time.sleep(1.0)
        assert len(polls()) == 1
        receiver = ReceiverProcess(tmp_path, f"http://127.0.0.1:{a.wake_port}")

        # --- a real delivery: forwarded, one poll within seconds, one event with its id ---
        sent = time.time()
        status, verdict = receiver.deliver(*github_delivery("d-real-1"))
        assert (status, verdict["outcome"]) == (202, "forwarded"), verdict
        wait_for(lambda: len(polls()) == 2, timeout=PROMPT_S,
                 what=lambda: f"the woken poll within {PROMPT_S}s\n{a.dump()}\n{b.dump()}\n{receiver.log()}")
        assert WAKE_SETTLE_S * 0.9 < polls()[1] - sent < PROMPT_S  # after the settle (E6), well before the interval
        (woken,) = woken_events()
        assert woken.payload == {"repository": REPOSITORY, "kind": "issues", "delivery": "d-real-1"}

        # --- a replay: the same delivery again — dropped at the receiver ---
        status, verdict = receiver.deliver(*github_delivery("d-real-1"))
        assert (status, verdict["outcome"]) == (200, "replayed"), verdict
        # --- forgeries: a wrong secret, an App the receiver does not know ---
        status, verdict = receiver.deliver(*github_delivery("d-forged-1", secret="guess"))
        assert (status, verdict["outcome"]) == (401, "forged"), verdict
        status, verdict = receiver.deliver(*github_delivery("d-forged-2", app="1"))
        assert (status, verdict["outcome"]) == (401, "forged"), verdict
        time.sleep(PROMPT_S)  # long enough for a wake to have shown, had one been forwarded
        assert len(polls()) == 2, f"a replay or a forgery caused a poll\n{a.dump()}\n{b.dump()}"
        assert len(woken_events()) == 1

        # --- a second real delivery: exactly one more poll ---
        sent = time.time()
        status, verdict = receiver.deliver(*github_delivery("d-real-2", event="pull_request"))
        assert (status, verdict["outcome"]) == (202, "forwarded"), verdict
        wait_for(lambda: len(polls()) == 3, timeout=PROMPT_S, what=lambda: f"the second woken poll\n{a.dump()}\n{b.dump()}")
        assert polls()[2] - sent < PROMPT_S
        time.sleep(1.0)
        assert len(polls()) == 3  # one wake, one poll — the lease still makes the two replicas poll once
        assert [e.payload["delivery"] for e in woken_events()] == ["d-real-1", "d-real-2"]
        assert a.proc.poll() is None and b.proc.poll() is None, a.dump() + b.dump()
    finally:
        if receiver is not None:
            receiver.stop()
        a_exit, b_exit = a.stop(), b.stop()
    assert (a_exit, b_exit) == (0, 0), a.dump() + b.dump()
    assert "Traceback" not in a.stderr_path.read_text() + b.stderr_path.read_text(), a.dump() + b.dump()

    # --- verified in logs: the receiver named every outcome, by delivery id ---
    log = receiver.log()
    def deliveries(outcome: str) -> list[str]:
        """The delivery ids the receiver logged under `event="delivery <outcome> …"`."""
        return [ln.split("delivery=")[1].split()[0] for ln in log.splitlines() if f'event="delivery {outcome}' in ln]

    assert deliveries("forwarded") == ["d-real-1", "d-real-2"], log
    assert deliveries("replayed") == ["d-real-1"], log
    assert sorted(deliveries("forged")) == ["d-forged-1", "d-forged-2"], log
    # and the control plane's side: the endpoint logged the two wakes it took, no more
    a_log = a.stderr_path.read_text()
    assert a_log.count("product woken") == 2, a_log
