"""The org's kill switch, driven the way an operator drives it (E3, #34).

`stop` and `resume` are the two verbs over one ledger row; `_serve` is the loop
that reads it. The store-level contract (CAS, audit, event, both backends) is
`test_ledger_contract.py`'s and the tick's reading of the row is the scheduler
and fleet suites'. This file tests the operator surface — what the verbs print,
what they refuse, that the verb writes a row and nothing else — and the loop's
own shutdown: a signal sets the engine's flag, the tick completes, the loop exits,
and the ledger is not told, because a restarted replica is not a stopped org.
"""

import os
import signal
import threading
import time

import pytest
from control_plane import cli
from control_plane.models import BurstRecord, TaskRecord, TaskStatus
from control_plane.store import SqliteLedger

REPOSITORY = "owner/product"


def run(tmp_path, *argv):
    args = cli.build_parser().parse_args([*argv, "--data-dir", str(tmp_path / "data")])
    return (cli._stop if args.command == "stop" else cli._resume)(args)


def test_stop_writes_the_row_and_names_the_live_bursts(tmp_path, capsys):
    ledger = SqliteLedger(tmp_path / "data" / "ledger.sqlite3")
    ledger.upsert_task(TaskRecord(task_id=f"{REPOSITORY}#1", repository=REPOSITORY, issue_number=1,
                                  status=TaskStatus.WORKING, claimed_by="host-a:41"))
    ledger.record_burst(BurstRecord(burst_id="b1", task_id=f"{REPOSITORY}#1"))

    assert run(tmp_path, "stop", "--reason", "incident #12", "--actor", "maksim") == 0
    out = capsys.readouterr().out
    assert "org stopped: incident #12" in out
    assert "1 live burst(s)" in out and "b1" in out and "held by host-a:41" in out
    assert "resume" in out
    stop = ledger.org_stop()
    assert (stop.reason, stop.stopped_by) == ("incident #12", "maksim")
    assert not (tmp_path / "data" / "STOP").exists()  # no file, no docker kill — the row is the whole verb
    assert [(a.actor, a.action, a.target) for a in ledger.audit_trail("org")] == [("maksim", "org.stop", "org")]


def test_a_second_stop_is_refused_and_says_what_stands(tmp_path, capsys):
    assert run(tmp_path, "stop", "--reason", "first", "--actor", "a") == 0
    assert run(tmp_path, "stop", "--reason", "second", "--actor", "b") == 2
    err = capsys.readouterr().err
    assert "already stopped" in err and "by a: first" in err
    ledger = SqliteLedger(tmp_path / "data" / "ledger.sqlite3")
    assert len(ledger.audit_trail("org")) == 1  # the refusal left no trail


def test_stop_requires_a_reason(tmp_path):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["stop", "--data-dir", str(tmp_path)])


def test_resume_lifts_the_stop_and_prints_the_trail(tmp_path, capsys):
    assert run(tmp_path, "resume", "--actor", "op") == 2  # nothing to lift
    assert "not stopped" in capsys.readouterr().err
    assert run(tmp_path, "stop", "--reason", "drill", "--actor", "op") == 0
    assert run(tmp_path, "resume", "--reason", "drill over", "--actor", "op") == 0
    out = capsys.readouterr().out
    assert "org resumed" in out and "org.stop — drill" in out and "org.resume — drill over" in out
    assert SqliteLedger(tmp_path / "data" / "ledger.sqlite3").org_stop() is None


def test_the_actor_defaults_to_the_os_login(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "login-name")
    assert run(tmp_path, "stop", "--reason", "drill") == 0
    ledger = SqliteLedger(tmp_path / "data" / "ledger.sqlite3")
    assert ledger.org_stop().stopped_by == "login-name"


# --- the loop's own shutdown (cli._serve) ---


class Engine:
    """What `_serve` needs of a Scheduler or a Fleet: a tick, a halt flag and
    the wake flag the sleep waits on (E5)."""

    def __init__(self, on_tick=None):
        self.halt = threading.Event()
        self.wake = threading.Event()
        self.ticks = 0
        self.on_tick = on_tick

    def shut_down(self):
        """What the signal handler does: the halt, and the wake that ends the sleep."""
        self.halt.set()
        self.wake.set()

    def tick(self):
        self.ticks += 1
        if self.on_tick is not None:
            self.on_tick(self)


def test_once_is_one_tick(tmp_path):
    engine = Engine()
    assert cli._serve(engine, interval=0, once=True) == 0
    assert engine.ticks == 1 and not engine.halt.is_set()


def test_a_signal_sets_the_flag_and_the_loop_exits_after_the_tick(tmp_path):
    """SIGTERM mid-tick: the handler sets `halt`, the tick it interrupted runs to
    its end, the loop breaks — no further tick, no ledger write anywhere."""
    def signal_self(engine):
        os.kill(os.getpid(), signal.SIGTERM)  # mid-tick, from "outside"
        engine.saw_halt_mid_tick = engine.halt.is_set()

    engine = Engine(on_tick=signal_self)
    before = signal.getsignal(signal.SIGTERM)
    try:
        assert cli._serve(engine, interval=30, once=False) == 0  # 30 s: an interval the exit must not wait for
    finally:
        signal.signal(signal.SIGTERM, before)
    assert engine.ticks == 1 and engine.halt.is_set()
    assert engine.saw_halt_mid_tick  # the flag is up while the tick still runs — the heartbeat reads it there


def test_the_sleep_is_cut_short_by_the_flag():
    """The loop waits on the flag, not the clock, so a shutdown during the
    interval is a prompt exit, not a `--interval` later."""
    engine = Engine()
    timer = threading.Timer(0.05, engine.shut_down)
    timer.start()
    assert cli._serve(engine, interval=30, once=False) == 0
    assert engine.ticks == 1


def test_a_wake_cuts_the_sleep_and_the_next_tick_follows(tmp_path):
    """E5: a webhook's wake sets the flag during the interval; the loop ticks
    now instead of `--interval` later, and then sleeps again — the flag is
    consumed, not left set (or the loop would spin)."""
    engine = Engine()
    ticked_at = []

    def stamp(engine):
        ticked_at.append(time.monotonic())
        if engine.ticks == 2:
            engine.shut_down()

    engine.on_tick = stamp
    threading.Timer(0.1, engine.wake.set).start()
    started = time.monotonic()
    assert cli._serve(engine, interval=30, once=False) == 0
    assert engine.ticks == 2
    assert 0.05 < ticked_at[1] - started < 5.0  # the second tick came with the wake, not the interval
