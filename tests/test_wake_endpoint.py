"""The control plane's internal wake endpoint (E5): `POST /wake` is one ledger
write and one nudge of the loop, nothing else.

In-process, on the single-node ledger: the endpoint is the same class a
replica runs, bound to an ephemeral port. Its multi-replica half — the
ledger's wake channel — is the contract suite's (`test_ledger_contract.py`),
and the whole path, receiver to poll, is `test_wakes.py`.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest
from control_plane import wake as wake_mod
from control_plane.models import ProductState, RepositoryRecord
from control_plane.store import SqliteLedger
from control_plane.wake import WakeEndpoint, attach_wakes

REPOSITORY = "owner/product"


class Engine:
    def __init__(self):
        self.wake = threading.Event()
        self.halt = threading.Event()


@pytest.fixture
def rig(tmp_path):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    ledger.register_repository(RepositoryRecord(repository=REPOSITORY, team="owners"), actor="fixture")
    engine = Engine()
    # the endpoint's own handle on the same file — it writes from the server
    # thread, and a single-node connection is bound to the thread that opened it
    own = SqliteLedger(tmp_path / "ledger.sqlite3")
    endpoint = WakeEndpoint("127.0.0.1:0", own, engine.wake).start()
    yield ledger, engine, endpoint, own
    endpoint.close()
    own.close()
    ledger.close()


def call(endpoint, method="POST", path="/wake", body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{endpoint.port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_wake_is_one_ledger_write_and_one_nudge(rig):
    ledger, engine, endpoint, _ = rig
    assert ledger.claim_observation(REPOSITORY, "host-a:1", 60) is True  # a live lease, as mid-interval
    events_before = len(ledger.events())

    status, body = call(endpoint, body={"repository": REPOSITORY, "kind": "issues", "delivery": "d-1"})
    assert (status, body) == (202, {"woken": True})
    assert engine.wake.is_set()  # the loop's sleep is over
    row = ledger.get_repository(REPOSITORY)
    assert row.claimed_by is None  # taken from its holder — the next look, after the settle, polls
    assert ledger.claim_observation(REPOSITORY, "host-b:1", 60) is False  # settling (E6): hands off for now
    (woken,) = ledger.events()[events_before:]
    assert woken.kind == "product_woken"
    assert woken.payload == {"repository": REPOSITORY, "kind": "issues", "delivery": "d-1"}


def test_a_product_not_observed_is_not_woken_and_the_loop_is_not_nudged(rig):
    ledger, engine, endpoint, _ = rig
    status, body = call(endpoint, body={"repository": "ghost/none", "kind": "push"})
    assert status == 404 and body["woken"] is False
    ledger.set_repository_state(REPOSITORY, ProductState.PAUSED, "op", reason="rehearsal")
    status, _ = call(endpoint, body={"repository": REPOSITORY, "kind": "push"})
    assert status == 404
    assert not engine.wake.is_set()
    assert [e.kind for e in ledger.events()] == ["product_registered", "product_paused"]


def test_a_malformed_wake_is_refused(rig):
    _, engine, endpoint, _ = rig
    assert call(endpoint, body={"kind": "push"})[0] == 400  # no repository
    assert call(endpoint, body={"repository": "not-a-slug", "kind": "push"})[0] == 400
    assert call(endpoint, body=["a", "list"])[0] == 400
    assert call(endpoint, path="/elsewhere", body={"repository": REPOSITORY})[0] == 404
    assert not engine.wake.is_set()


def test_healthz_is_the_only_read(rig):
    _, _, endpoint, _ = rig
    assert call(endpoint, method="GET", path="/healthz") == (200, {"ok": True})
    assert call(endpoint, method="GET", path="/wake")[0] == 404


def test_a_ledger_failure_is_a_lost_wake_not_a_dead_endpoint(rig, monkeypatch):
    """The wake is best-effort — the poll is the truth — so a store error is
    logged and answered, and the endpoint keeps serving."""
    _, engine, endpoint, own = rig

    def boom(*args, **kwargs):
        raise RuntimeError("ledger away")

    monkeypatch.setattr(own, "wake_repository", boom)
    status, _ = call(endpoint, body={"repository": REPOSITORY, "kind": "push"})
    assert status == 404 and not engine.wake.is_set()
    monkeypatch.undo()
    assert call(endpoint, body={"repository": REPOSITORY, "kind": "push"})[0] == 202


def test_attach_wires_the_channel_and_binds_only_when_asked(tmp_path, monkeypatch):
    """`attach_wakes`: the ledger's channel is always subscribed (a peer's wake
    must reach this loop); the endpoint exists only when WAKE_BIND says so —
    unset is polls only, the laptop's shape, and nothing listens."""
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    subscribed = []
    monkeypatch.setattr(ledger, "subscribe_wakes", lambda on_wake: subscribed.append(on_wake))
    engine = Engine()

    monkeypatch.delenv(wake_mod.WAKE_BIND_ENV, raising=False)
    assert attach_wakes(engine, ledger) is None
    assert len(subscribed) == 1
    subscribed[0]("owner/product")  # what the channel does on a peer's wake
    assert engine.wake.is_set()

    engine.wake.clear()
    monkeypatch.setenv(wake_mod.WAKE_BIND_ENV, "127.0.0.1:0")
    endpoint = attach_wakes(engine, ledger)
    try:
        assert endpoint is not None and endpoint.port > 0
        assert call(endpoint, method="GET", path="/healthz")[0] == 200
    finally:
        endpoint.close()
    ledger.close()
