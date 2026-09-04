"""E2's done-when (#33): two control-plane processes over one Postgres, one of
them killed mid-burst, and zero doubled bursts. And E3's (#34), on the same rig:
a `stop` issued from a third process kills a burst one replica holds, through the
ledger row, the heartbeat and the runner — and `resume` picks the work back up
with no process restarted.

Two `replica_harness.py` processes serve the same product (registered with
`concurrency=2`, so both work at once) from one database. When the first
replica's runner reports a burst under way, the test SIGKILLs it — the crash a
lease is designed for: no handler runs, no lock is released by code, the
connection just vanishes. The survivor must finish every task, including the
dead replica's, and must do so *after* that lease lapsed, never on sight.

What "zero doubled bursts" means here, and where each half is read from:

- the ledger — every task ends `awaiting_review` with a PR, one `succeeded`
  burst each; the dead replica's burst is the one `killed` row, and its task
  carries the crash as one failed attempt;
- the replicas' own event logs — the dead one started one burst and finished
  none; the survivor started and finished each task exactly once; the survivor's
  burst on the orphaned task began a full lease after the dead replica's did;
- the observation lease — across both processes, GitHub was polled at most once
  per interval.

Postgres only: two processes on one SQLite file is not the shape the platform
runs, and the single-node ledger is retired as an origin (A6). The stores CI job
is where this runs; `LEDGER_DSN` unset skips it.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from control_plane.migrate import migrate
from control_plane.models import BurstStatus, RepositoryRecord, TaskStatus
from control_plane.postgres import PostgresLedger
from control_plane.store import LEDGER_DSN_ENV
from psycopg.conninfo import make_conninfo

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason=f"{LEDGER_DSN_ENV} unset")

HARNESS = Path(__file__).with_name("replica_harness.py")
REPOSITORY = "owner/product"
ISSUES = 4
BURST_S = 1.5
LEASE_S = 3
POLL_INTERVAL_S = 1
INTERVAL = 0.2


@pytest.fixture()
def fresh_dsn():
    """A migrated database of this test's own, with the product registered."""
    name = f"replicas_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    ledger = PostgresLedger(dsn)
    ledger.register_repository(
        RepositoryRecord(repository=REPOSITORY, team="owners", concurrency=2, daily_bursts=50), actor="test"
    )
    ledger.close()
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def hermetic_env(**extra) -> dict[str, str]:
    """The environment a spawned process gets: this one's, minus the platform's
    own GitHub/OTel configuration (the fakes need none), plus ``extra``."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "OTEL_"))} | extra


class Replica:
    """One harness process and the files it writes."""

    def __init__(self, name: str, root: Path, dsn: str, burst_s: float = BURST_S):
        self.name = name
        self.data_dir = root / name
        self.data_dir.mkdir()
        archive = root / "archive"  # shared, like the ledger: a task may change hands between bursts
        archive.mkdir(exist_ok=True)
        self.events_path = root / f"{name}.events"
        self.stderr_path = root / f"{name}.stderr"
        self._stderr = self.stderr_path.open("w")
        self.proc = subprocess.Popen(
            [
                sys.executable, str(HARNESS),
                "--dsn", dsn, "--data-dir", str(self.data_dir), "--archive-dir", str(archive),
                "--events", str(self.events_path),
                "--issues", str(ISSUES), "--burst-s", str(burst_s), "--interval", str(INTERVAL),
                "--lease-s", str(LEASE_S), "--poll-interval-s", str(POLL_INTERVAL_S),
            ],
            stdout=subprocess.DEVNULL,
            stderr=self._stderr,
            env=hermetic_env(),
        )

    @property
    def replica(self) -> str:
        """The name on this process's leases (`host:pid`), as it announced itself."""
        return self.events("up")[0]["replica"]

    def events(self, kind: str | None = None) -> list[dict]:
        if not self.events_path.exists():
            return []
        rows = [json.loads(line) for line in self.events_path.read_text().splitlines() if line.strip()]
        return [row for row in rows if kind is None or row["kind"] == kind]

    def kill(self) -> float:
        """SIGKILL — no handler, no cleanup; the crash a lease is for."""
        at = time.time()
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=10)
        return at

    def stop(self) -> int | None:
        """SIGTERM — the shutdown the loop honors after its tick (E3: a process
        flag, not an org STOP); None if it was already gone."""
        if self.proc.poll() is not None:
            self._stderr.close()
            return None
        self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=10)
        finally:
            self._stderr.close()

    def dump(self) -> str:
        if self.proc.poll() is None:  # still up: ask it where it is (faulthandler on SIGUSR1, in the harness)
            self.proc.send_signal(signal.SIGUSR1)
            time.sleep(0.5)
        return f"--- {self.name} stderr ---\n{self.stderr_path.read_text()}\n--- {self.name} events ---\n" + "\n".join(
            json.dumps(e) for e in self.events()
        )


def wait_for(condition, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def test_two_replicas_one_killed_mid_burst_the_survivor_absorbs_exactly_once(fresh_dsn, tmp_path):
    a = Replica("a", tmp_path, fresh_dsn)
    b = Replica("b", tmp_path, fresh_dsn)
    ledger = PostgresLedger(fresh_dsn)

    def tasks():
        return {t.issue_number: t for t in ledger.tasks_by_status(*TaskStatus, repository=REPOSITORY)}

    try:
        # --- the crash: A is mid-burst when it dies ---
        started = wait_for(lambda: a.events("start"), timeout=45, what="replica a to start a burst")
        assert a.proc.poll() is None, a.dump()
        killed_at = a.kill()
        orphan = started[0]["task"]

        # --- the survivor finishes everything, the orphan included ---
        def all_done():
            rows = tasks()
            return len(rows) == ISSUES and all(
                t.status == TaskStatus.AWAITING_REVIEW and t.pr_number for t in rows.values()
            )

        wait_for(all_done, timeout=60, what=f"every task to reach awaiting_review\n{a.dump()}\n{b.dump()}")
        assert b.proc.poll() is None, b.dump()  # still serving — nothing in the tick raised
    finally:
        a.stop()
        b_exit = b.stop()
    assert b_exit == 0, b.dump()
    assert "Traceback" not in b.stderr_path.read_text(), b.dump()

    # --- the ledger: one success per task, one killed row, nothing left running ---
    rows = tasks()
    for task in rows.values():
        assert (task.claimed_by, task.claimed_until) == (None, None)  # every lease released
    task_ids = sorted(t.task_id for t in rows.values())
    bursts = [ledger.get_burst(e["burst"]) for e in a.events("start") + b.events("start")]
    by_status: dict[str, list[str]] = {}
    for burst in bursts:
        by_status.setdefault(str(burst.status), []).append(burst.task_id)
    assert sorted(by_status.get(str(BurstStatus.SUCCEEDED), [])) == task_ids  # one success per task
    assert by_status.get(str(BurstStatus.KILLED)) == [orphan]  # A's, reconciled by B
    assert str(BurstStatus.RUNNING) not in by_status
    assert ledger.running_bursts(repository=REPOSITORY) == []
    attempts = {t.task_id: t.attempts for t in rows.values()}
    assert attempts[orphan] == 1 and all(n == 0 for tid, n in attempts.items() if tid != orphan)

    # --- the replicas' own records: each task's work happened once ---
    assert len(a.events("start")) == 1 and a.events("finish") == [] and a.events("fenced") == []
    assert sorted(e["task"] for e in b.events("start")) == task_ids  # no restarts
    assert sorted(e["task"] for e in b.events("finish")) == task_ids  # every one to the end
    assert b.events("fenced") == []
    assert all(e["t"] < killed_at for e in a.events())  # A wrote nothing after it died

    # --- and the survivor waited the lease out: the orphan's second burst began
    # a full lease after the first (whose `start` line follows a renewal), never
    # on sight of a dead peer ---
    a_start = next(e for e in a.events("start") if e["task"] == orphan)["t"]
    b_start = next(e for e in b.events("start") if e["task"] == orphan)["t"]
    assert b_start - a_start >= LEASE_S - 0.3, (a_start, b_start)
    assert b_start >= killed_at  # (and, trivially, after the kill)

    # --- the observation lease: one poll per interval, fleet-wide. Without it
    # the gaps would be the tick interval, from either replica ---
    observed = sorted(e["t"] for e in a.events("observe") + b.events("observe"))
    assert len(observed) >= 3
    gaps = [later - earlier for earlier, later in zip(observed, observed[1:])]
    assert min(gaps) >= POLL_INTERVAL_S * 0.6 > INTERVAL, gaps
    ledger.close()


# --- E3 (#34): a stop issued elsewhere kills a burst spawned here ---


def operator(dsn: str, *argv: str) -> tuple[int, str]:
    """The operator's CLI, as its own process over the same ledger — the third
    party to the two replicas, holding no burst and no container, with nothing
    but the row to act through."""
    done = subprocess.run(
        [sys.executable, "-c", "import sys; from control_plane.cli import main; sys.exit(main(sys.argv[1:]))", *argv],
        capture_output=True, text=True, timeout=60,
        env=hermetic_env(**{LEDGER_DSN_ENV: dsn}),
    )
    return done.returncode, done.stdout + done.stderr


def test_a_stop_issued_elsewhere_kills_the_burst_and_resume_picks_the_work_back_up(fresh_dsn, tmp_path):
    """The done-when, verbatim: replica A spawns a burst; the stop comes from
    another process — the operator's verb, which can no more reach A's container
    than replica B could — and the burst dies, killed by A's own runner when A's
    heartbeat reads the row. Then everyone stands down until `resume`, and the
    work completes without a single process restarted — the killed task's next
    burst resuming from the snapshot the killed one archived, on whichever
    replica picks it up (the archive is shared, like the ledger; #87 is what
    happens when it does not hold the blob)."""
    long_burst = 6.0  # long enough that the operator's process is up (~0.5 s here, slower in CI) before it ends
    a = Replica("a", tmp_path, fresh_dsn, burst_s=long_burst)
    b = Replica("b", tmp_path, fresh_dsn, burst_s=long_burst)
    ledger = PostgresLedger(fresh_dsn)

    def tasks():
        return {t.task_id: t for t in ledger.tasks_by_status(*TaskStatus, repository=REPOSITORY)}

    def started_by(replica):
        return replica.events("start")

    try:
        # --- A is mid-burst; the stop is issued from a third process ---
        started = wait_for(lambda: started_by(a) or started_by(b), timeout=45, what="a first burst")
        holder, other = (a, b) if started_by(a) else (b, a)
        victim = started[0]
        assert ledger.get_task(victim["task"]).claimed_by == holder.replica  # A holds it, by its lease
        code, out = operator(fresh_dsn, "stop", "--reason", "drill: E3 done-when", "--actor", "drill-operator")
        stop_landed = time.time()
        assert code == 0, out
        assert "org stopped: drill: E3 done-when" in out
        assert victim["burst"] in out and f"held by {holder.replica}" in out  # the verb names the burst it cannot reach
        live_at_stop = [e for e in holder.events("start") + other.events("start")
                        if e["t"] < stop_landed and e["burst"] not in {f["burst"] for f in holder.events("finish") + other.events("finish")}]
        assert victim in live_at_stop

        # --- the kill: A's runner fences it, the row shows killed, nothing runs ---
        wait_for(lambda: ledger.running_bursts(repository=REPOSITORY) == [], timeout=30,
                 what=f"every live burst to be fenced\n{a.dump()}\n{b.dump()}")
        killed_at = time.time()
        for e in live_at_stop:
            burst = ledger.get_burst(e["burst"])
            assert burst.status == BurstStatus.KILLED and burst.exit_code == 137, burst
            assert e["burst"] in {f["burst"] for f in (a if e["pid"] == a.proc.pid else b).events("fenced")}
            task = ledger.get_task(e["task"])
            assert (task.status, task.attempts, task.claimed_by) == (TaskStatus.CLAIMED, 1, None)  # rolled back, one attempt
        assert killed_at - stop_landed < 2.0  # within a heartbeat, not a lease

        # --- standing down: no spawn, no poll, both processes alive ---
        time.sleep(1.5)  # several ticks at the interval
        quiet_after = stop_landed + 0.5  # a tick already past its top check may finish its observe
        late = [e for e in a.events() + b.events() if e["t"] > quiet_after and e["kind"] in ("start", "observe")]
        assert late == [], late
        assert a.proc.poll() is None and b.proc.poll() is None, a.dump() + b.dump()

        # --- resume, from the same third process; the loops pick up as they are ---
        resume_issued = time.time()  # the row may flip any moment after this
        code, out = operator(fresh_dsn, "resume", "--reason", "drill over", "--actor", "drill-operator")
        assert code == 0 and "org resumed" in out, out
        assert [e for e in a.events("start") + b.events("start") if quiet_after < e["t"] < resume_issued] == []

        def all_done():
            rows = tasks()
            return len(rows) == ISSUES and all(
                t.status == TaskStatus.AWAITING_REVIEW and t.pr_number for t in rows.values()
            )

        wait_for(all_done, timeout=90, what=f"every task to reach awaiting_review after resume\n{a.dump()}\n{b.dump()}")
    finally:
        a_exit = a.stop()
        b_exit = b.stop()
    assert (a_exit, b_exit) == (0, 0), a.dump() + b.dump()
    for replica in (a, b):
        assert "Traceback" not in replica.stderr_path.read_text(), replica.dump()

    # --- the ledger's account: one success per task, the kills counted, the verbs on record ---
    rows = tasks()
    task_ids = sorted(rows)
    bursts = [ledger.get_burst(e["burst"]) for e in a.events("start") + b.events("start")]
    succeeded = sorted(bu.task_id for bu in bursts if bu.status == BurstStatus.SUCCEEDED)
    killed = [bu for bu in bursts if bu.status == BurstStatus.KILLED]
    assert succeeded == task_ids  # every task exactly once, the killed one included
    assert sorted(bu.burst_id for bu in killed) == sorted(e["burst"] for e in live_at_stop)  # killed = what was live at the stop
    assert all(bu.status != BurstStatus.RUNNING for bu in bursts)
    for task in rows.values():
        assert task.attempts == sum(1 for bu in killed if bu.task_id == task.task_id)  # a kill is one attempt, nothing else is
        assert (task.claimed_by, task.claimed_until) == (None, None)
    assert [(e.actor, e.action, e.reason) for e in ledger.audit_trail("org")] == [
        ("drill-operator", "org.stop", "drill: E3 done-when"),
        ("drill-operator", "org.resume", "drill over"),
    ]
    ledger.close()
