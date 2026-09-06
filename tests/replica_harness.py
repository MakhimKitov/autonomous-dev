"""One control-plane replica, as a process — the piece `test_replicas.py` spawns twice.

The engine is real from the loop down: `cli._serve` driving a `Fleet` that builds
a `Scheduler` per registry row, `execute_burst` with its volume and ledger
choreography, `PostgresLedger` on the database the test made. Two seats are
fakes, the same two `test_transactions.py` fakes: GitHub is a script that keeps
the product's issues `ready` and answers every dev branch with a PR, and
docker's seat is a runner that sleeps for the burst's length while asking the
heartbeat, the way `DockerRunner` asks it on the clock.

It runs as a process, not a thread, so that a SIGKILL from the test is exactly
what a crashed replica is: a connection that vanishes mid-statement, a lease
nobody renews, a burst row left `running`. What each replica did is written to
an events file — one JSON object per line, wall-clock stamped — which is what
the test reads to prove nothing ran twice. Replicas share the ledger and the
session archive (`--archive-dir`), as a deployment's do; everything else under
`--data-dir` is the process's own.

Not collected by pytest (no `test_` prefix); run with the interpreter of the
environment under test:

    python tests/replica_harness.py --dsn … --data-dir … --events … [rails]
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

from control_plane.bursts import PlatformStores
from control_plane.cli import _serve
from control_plane.github_api import Issue, PullRequest
from control_plane.postgres import PostgresLedger
from control_plane.scheduler import Fleet, Rails, Scheduler, default_replica
from control_plane.store import LocalSessionArchive
from control_plane.telemetry import init_telemetry
from dev_agent.log import configure_logging
from control_plane.wake import attach_wakes


class Events:
    """The replica's own record: appended and flushed per line, so the last
    line before a SIGKILL is on disk."""

    def __init__(self, path: Path):
        self._file = path.open("a", buffering=1)

    def write(self, kind: str, **fields) -> None:
        self._file.write(json.dumps({"t": time.time(), "pid": os.getpid(), "kind": kind, **fields}) + "\n")
        self._file.flush()


class ScriptedGitHub:
    """A product with `issues` open tasks, all `ready`, forever — the label
    edits are accepted and forgotten, which the scheduler tolerates (a task it
    already holds is skipped as label drift). Every dev branch has a PR."""

    def __init__(self, repo: str, issues: int, events: Events):
        self.repo = repo
        self._issues = issues
        self._events = events

    def list_ready_issues(self):
        self._events.write("observe")
        return [self.get_issue(n) for n in range(1, self._issues + 1)]

    def get_issue(self, number):
        return Issue(number=number, title=f"task {number}", body="do it", labels=["ready", "human"])

    def add_labels(self, number, labels):
        pass

    def remove_label(self, number, label):
        pass

    def comment(self, number, body):
        pass

    def open_pr_with_branch_prefix(self, prefix):
        number = int(prefix.split("/", 1)[1].split("-", 1)[0])  # 'dev/{issue}-'
        return self.get_pr(100 + number)

    def get_pr(self, number):
        return PullRequest(number=number, state="open", merged=False, head_ref=f"dev/{number - 100}-x", head_sha="abc")

    def list_reviews(self, pr_number):
        return []

    def list_review_comments(self, pr_number):
        return []

    def request_reviewers(self, pr_number, reviewers):
        return []


class SleepingRunner:
    """Docker's seat (a `Runner`, E4): a burst is `seconds` of wall clock with
    the heartbeat asked throughout; a False fences it, as `DockerRunner` would
    kill the container. Nothing is ever `alive` here — no containers exist, so
    the reconciler judges by the lease alone."""

    def __init__(self, seconds: float, events: Events):
        self._seconds = seconds
        self._events = events

    def run(self, spec, log_path=None, heartbeat=None):
        def alive() -> bool:
            if heartbeat is None or heartbeat():
                return True
            self._events.write("fenced", burst=spec.burst_id, task=spec.task_id)
            return False

        # the first beat lands before the `start` line, so the lease the test
        # reasons about is at least as fresh as that stamp
        if not alive():
            return 137
        self._events.write("start", burst=spec.burst_id, task=spec.task_id)
        deadline = time.monotonic() + self._seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if not alive():
                return 137
        self._events.write("finish", burst=spec.burst_id, task=spec.task_id)
        return 0

    def alive(self, burst_id):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--data-dir", type=Path, required=True, help="this replica's own: volumes, logs, clone")
    parser.add_argument("--archive-dir", type=Path, default=None,
                        help="the session archive, shared by every replica like the ledger is (default: the data dir)")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--issues", type=int, default=4)
    parser.add_argument("--burst-s", type=float, default=1.5)
    parser.add_argument("--interval", type=float, default=0.2, help="the loop's sleep (`run --interval`)")
    parser.add_argument("--lease-s", type=int, default=3)
    parser.add_argument("--poll-interval-s", type=int, default=1, help="the observation lease's span")
    parser.add_argument("--wake-bind", default=None,
                        help="host:port for this replica's internal wake endpoint (E5; `WAKE_BIND`); "
                             "port 0 binds an ephemeral one, announced in the `up` event")
    args = parser.parse_args(argv)

    # a replica that goes quiet can be asked where it is: `kill -USR1 <pid>`
    # dumps every thread's stack to stderr (the test keeps stderr per replica)
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        os.environ.pop(name, None)  # a dev-mode burst: token-less, like the fakes
    configure_logging()  # logfmt on stdout, as a replica's is; the test that wants it captures it
    init_telemetry()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    events = Events(args.events)
    # the two stores the replicas share are the ledger and the archive — a task's
    # next burst resumes from the snapshot its last one left, whichever replica
    # ran it; the data dir (volumes, logs) is this process's own
    stores = PlatformStores(
        data_dir=args.data_dir,
        archive=LocalSessionArchive(args.archive_dir or args.data_dir),
        ledger=PostgresLedger(args.dsn),
    )
    runner = SleepingRunner(args.burst_s, events)  # docker's seat, through the seam
    rails = Rails(
        failure_cooldown_s=0,
        lease_s=args.lease_s,
        poll_interval_s=args.poll_interval_s,  # what cli.py does with the loop's interval
    )
    repo_dir = args.data_dir / "clone"
    repo_dir.mkdir(exist_ok=True)

    def build_scheduler(row):
        # cli._run_fleet's build, minus the App: the row's quotas into the rails
        return Scheduler(
            stores,
            ScriptedGitHub(row.repository, args.issues, events),
            repo_dir,
            row.team,
            rails=replace(rails, daily_burst_ceiling=row.daily_bursts, concurrency=row.concurrency),
            sync_clone=lambda: repo_dir,
            runner=runner,
        )

    fleet = Fleet(stores, build_scheduler)
    # the wake wiring cli._run_fleet does: the ledger's channel always, the
    # endpoint when asked — on a handle of its own, as the endpoint's thread needs
    endpoint = attach_wakes(fleet, PostgresLedger(args.dsn), args.wake_bind or "")
    events.write("up", replica=default_replica(), wake_port=endpoint.port if endpoint else None)
    return _serve(fleet, args.interval, once=False)


if __name__ == "__main__":
    sys.exit(main())
