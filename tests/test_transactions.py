"""E1's done-when: no code path holds a transaction across I/O.

A full scheduler tick runs over each backend — the ledger and the burst
choreography are real, GitHub and docker are fakes — and the fakes are the
assertion: every I/O seam (each GitHub call, the clone sync, the container run,
each heartbeat inside it) checks that the ledger connection is idle the moment
it is entered, and every ledger method checks that it is idle the moment it
returns. A transaction left open anywhere in the tick fails here, on the
backend where it would have held a lock.

`in_transaction` is the hook (`SqliteLedger`, `PostgresLedger`); the per-method
version of this assertion is in the ledger contract suite.
"""

import os
from uuid import uuid4

import psycopg
import pytest
from control_plane.bursts import PlatformStores
from control_plane.github_api import Issue, PullRequest, Review
from control_plane.migrate import migrate
from control_plane.models import BurstRecord, BurstStatus, RepositoryRecord, TaskStatus, task_id_for
from control_plane.review import ReviewPolicy
from control_plane.runner import HostPhases
from control_plane.scheduler import Rails, Scheduler
from control_plane.store import LEDGER_DSN_ENV, LocalSessionArchive, SqliteLedger
from psycopg.conninfo import make_conninfo

DSN = os.environ.get(LEDGER_DSN_ENV, "").strip()
BACKENDS = ["sqlite"] + (["postgres"] if DSN else [])

OWNER = "owner1"
REPOSITORY = "owner1/product"


class IdleAfterEveryCall:
    """The ledger, with the post-condition every method must meet."""

    def __init__(self, ledger):
        self._ledger = ledger
        self.calls = 0

    @property
    def in_transaction(self):
        return self._ledger.in_transaction

    def __getattr__(self, name):
        attr = getattr(self._ledger, name)
        if not callable(attr):
            return attr

        def checked(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                self.calls += 1
                assert self._ledger.in_transaction is False, f"ledger.{name} returned mid-transaction"

        return checked


class IdleOnEntry:
    """Every I/O seam the tick crosses asserts the ledger is idle on the way in."""

    def __init__(self, ledger):
        self.ledger = ledger
        self.crossings = 0

    def probe(self, where):
        self.crossings += 1
        assert self.ledger.in_transaction is False, f"a transaction was open entering {where}"


class ProbingApi:
    repo = REPOSITORY

    def __init__(self, probe):
        self._probe = probe
        self.ready_issues = []
        self.prs = {}
        self.open_prs = []
        self.reviews = {}
        self.label_ops = []
        self.comments = []

    def list_ready_issues(self):
        self._probe.probe("list_ready_issues")
        return list(self.ready_issues)

    def get_issue(self, number):
        self._probe.probe("get_issue")
        return Issue(number=number, title="t", body="b", labels=["human"])

    def add_labels(self, number, labels):
        self._probe.probe("add_labels")
        self.label_ops.append(("add", number, tuple(labels)))

    def remove_label(self, number, label):
        self._probe.probe("remove_label")
        self.label_ops.append(("remove", number, label))

    def comment(self, number, body):
        self._probe.probe("comment")
        self.comments.append((number, body))

    def open_pr_with_branch_prefix(self, prefix):
        self._probe.probe("open_pr_with_branch_prefix")
        return next((pr for pr in self.open_prs if pr.head_ref.startswith(prefix)), None)

    def get_pr(self, number):
        self._probe.probe("get_pr")
        return self.prs[number]

    def list_reviews(self, pr_number):
        self._probe.probe("list_reviews")
        return self.reviews.get(pr_number, [])

    def list_review_comments(self, pr_number):
        self._probe.probe("list_review_comments")
        return []

    def request_reviewers(self, pr_number, reviewers):
        self._probe.probe("request_reviewers")
        return []


class ProbingRunner(HostPhases):
    """Docker's seat: the longest I/O of all, with the heartbeat asked inside it —
    and the reconciler's `alive`, the other I/O a runner does. The bytes phases
    (G1) are the real ones, so the volume I/O a tick does is probed too."""

    def __init__(self, probe, archive=None):
        self._probe = probe
        self.beats = 0
        self.archive = archive

    def run(self, spec, log_path=None, heartbeat=None):
        self._probe.probe("runner.run")
        assert heartbeat is not None  # every scheduler burst carries its lease renewal
        for _ in range(3):
            assert heartbeat() is True
            self._probe.probe("after a heartbeat")
            self.beats += 1
        return 0

    def alive(self, burst_id):
        self._probe.probe("runner.alive")
        return False


@pytest.fixture(scope="module")
def pg_dsn():
    if not DSN:
        yield None
        return
    name = f"transactions_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(DSN, dbname=name)
    migrate(dsn)
    try:
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(params=BACKENDS)
def rig(request, tmp_path, monkeypatch, pg_dsn):
    if request.param == "sqlite":
        raw = SqliteLedger(tmp_path / "ledger.sqlite3")
    else:
        from control_plane.postgres import PostgresLedger

        with psycopg.connect(pg_dsn, autocommit=True) as conn:  # one database for the module, clean per test
            conn.execute("TRUNCATE tasks, bursts, violations, events, memory_entries, repositories, audit")
        raw = PostgresLedger(pg_dsn)
    raw.register_repository(RepositoryRecord(repository=REPOSITORY, team="owners"), actor="fixture")
    ledger = IdleAfterEveryCall(raw)
    probe = IdleOnEntry(raw)
    stores = PlatformStores(data_dir=tmp_path, archive=LocalSessionArchive(tmp_path), ledger=ledger)
    api = ProbingApi(probe)
    runner = ProbingRunner(probe, archive=stores.archive)
    repo = tmp_path / "repo"
    repo.mkdir()

    def sync_clone():
        probe.probe("sync_clone")
        return repo

    scheduler = Scheduler(
        stores, api, repo, OWNER,
        rails=Rails(failure_cooldown_s=0, lease_s=0),  # lease_s=0: every heartbeat renews
        review=ReviewPolicy(enabled=True, providers=("copilot",)),
        sync_clone=sync_clone,
        runner=runner,  # the real choreography, with the probing runner in docker's seat
    )
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(name, raising=False)  # a dev-mode burst: token-less, like the fakes
    yield scheduler, api, stores, probe, runner
    raw.close()


def _pr(state="open", merged=False, head_sha="abc"):
    return PullRequest(number=2, state=state, merged=merged, head_ref="dev/1-x", head_sha=head_sha)


def test_the_whole_tick_crosses_every_io_seam_with_the_connection_idle(rig):
    scheduler, api, stores, probe, runner = rig
    tid = task_id_for(REPOSITORY, 1)

    # claim → work burst → PR → awaiting_review
    api.ready_issues = [Issue(number=1, title="t", body="b", labels=["ready", "human"])]
    api.open_prs = [_pr()]
    api.prs[2] = _pr()
    scheduler.tick()
    task = stores.ledger.get_task(tid)
    assert task.status == TaskStatus.AWAITING_REVIEW and task.pr_number == 2
    assert (task.claimed_by, task.claimed_until) == (None, None)
    assert runner.beats == 3

    # the owner asks for changes → review round queued and run in one tick → awaiting_review
    api.ready_issues = []
    api.reviews[2] = [Review(id=10, author=OWNER, state="CHANGES_REQUESTED", body="tweak")]
    scheduler.tick()
    task = stores.ledger.get_task(tid)
    assert (task.status, task.last_review_id, task.review_rounds) == (TaskStatus.AWAITING_REVIEW, 10, 1)
    assert runner.beats == 6

    # a dead replica's burst: orphaned working task + running row → killed, rolled back
    stores.ledger.upsert_task(
        stores.ledger.get_task(tid).__class__(
            task_id=task_id_for(REPOSITORY, 5), repository=REPOSITORY, issue_number=5,
            status=TaskStatus.WORKING, next_burst_kind="work",
            claimed_by="ghost:1", claimed_until="2020-01-01T00:00:00+00:00",
        )
    )
    stores.ledger.record_burst(BurstRecord(burst_id="dead", task_id=task_id_for(REPOSITORY, 5)))
    scheduler.reconcile_running()
    assert stores.ledger.get_burst("dead").status == BurstStatus.KILLED
    assert stores.ledger.get_task(task_id_for(REPOSITORY, 5)).status == TaskStatus.CLAIMED

    # merged → done
    api.prs[2] = _pr(state="closed", merged=True)
    scheduler.observe()
    assert stores.ledger.get_task(tid).status == TaskStatus.DONE

    # the hooks fired — this test is only as strong as the number of times it looked
    assert probe.crossings >= 25
    assert stores.ledger.calls >= 30


def test_the_hook_bites_on_a_transaction_left_open(rig, monkeypatch):
    """Negative control: a write that forgets to commit is caught at the next
    seam — otherwise the test above proves nothing about what it watches."""
    scheduler, api, stores, probe, runner = rig
    raw = stores.ledger._ledger
    if type(raw).__name__ != "SqliteLedger":
        pytest.skip("autocommit cannot be made to leak a transaction; the SQLite half is the control")
    original = type(raw).record_burst

    def leaks(self, burst):
        original(self, burst)
        self._conn.execute("UPDATE bursts SET kind = kind WHERE burst_id = ?", (burst.burst_id,))  # opens, never commits

    monkeypatch.setattr(type(raw), "record_burst", leaks)
    api.ready_issues = [Issue(number=1, title="t", body="b", labels=["ready", "human"])]
    api.open_prs = [_pr()]
    api.prs[2] = _pr()
    with pytest.raises(AssertionError, match="record_burst returned mid-transaction"):
        scheduler.tick()
