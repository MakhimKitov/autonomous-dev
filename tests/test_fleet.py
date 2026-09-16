"""The fleet (C4): one serial loop, N products, least-recently-served.

Schedulers are recording fakes — the fleet's job is *who* gets observed and
*who* gets the one burst, never what a burst does (test_scheduler.py owns
that). The registry rows are real, in a real single-node ledger, because the
fairness cursor and the active/paused gate live there.
"""

import pytest
from control_plane import cli
from control_plane.bursts import PlatformStores
from control_plane.github_api import GitHubApiError
from control_plane.models import (
    BurstRecord,
    BurstStatus,
    ProductState,
    RepositoryRecord,
    TaskRecord,
    task_id_for,
    utc_now,
)
from control_plane.review import ReviewPolicyError
from control_plane.scheduler import Fleet
from control_plane.subscriptions import declare

A = "owner/alpha"
B = "owner/beta"


@pytest.fixture(autouse=True)
def hermetic_platform_env(monkeypatch):
    for name in (
        "REVIEW",
        "REVIEW_PROVIDERS",
        "REVIEWER_MODEL",
        "E2E",
        "TESTER_MODEL",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "REVIEWER_GITHUB_APP_ID",
        "REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH",
        "TESTER_GITHUB_APP_ID",
        "TESTER_GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_COPILOT_REVIEW_TOKEN",
        "SCHEDULER_POLL_INTERVAL_S",
        "DEV_AGENT_MODEL",
        "SCHEDULER_BURST_BUDGET_USD",
        "SCHEDULER_BURST_MAX_TURNS",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeScheduler:
    """Records the fleet's calls; bursts on demand."""

    def __init__(self, slug: str, journal: list):
        self.slug = slug
        self.journal = journal
        self.claimable = False
        self.observe_error: Exception | None = None

    def observation_turn(self):
        # the lease itself is the real scheduler's (test_scheduler.py, E2); the
        # fleet's part is to ask for the turn, product by product
        self.journal.append(("reconcile", self.slug))
        if self.observe_error is not None:
            raise self.observe_error
        self.journal.append(("observe", self.slug))
        return True

    def pick_next(self):
        if not self.claimable:
            return None
        return TaskRecord(
            task_id=task_id_for(self.slug, 1), repository=self.slug, issue_number=1
        )

    def run_task_burst(self, task, capacity=None):
        # the fleet picks who pays once per tick (H1) and hands it down
        self.journal.append(("burst", self.slug))
        self.capacity = capacity


@pytest.fixture()
def rig(tmp_path):
    stores = PlatformStores.open(tmp_path / "data")
    journal: list = []
    fakes: dict[str, FakeScheduler] = {}
    build_errors: dict[str, Exception] = {}

    def build(row: RepositoryRecord) -> FakeScheduler:
        if row.repository in build_errors:
            raise build_errors[row.repository]
        journal.append(("build", row.repository))
        if row.repository not in fakes:
            fakes[row.repository] = FakeScheduler(row.repository, journal)
        return fakes[row.repository]

    return Fleet(stores, build), stores, fakes, journal, build_errors


def register(stores, slug, **kwargs):
    kwargs.setdefault("team", "bots")
    stores.ledger.register_repository(RepositoryRecord(repository=slug, **kwargs), "op")


def bursts(journal):
    return [slug for verb, slug in journal if verb == "burst"]


def test_a_tick_observes_everyone_and_bursts_at_most_once(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[A].claimable = fakes[B].claimable = True
    journal.clear()

    fleet.tick()
    assert ("observe", A) in journal and ("observe", B) in journal
    assert len(bursts(journal)) == 1  # work everywhere, still one burst per tick


def test_turns_go_to_the_least_recently_served(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()  # builds the fakes
    fakes[A].claimable = fakes[B].claimable = True

    for _ in range(4):
        fleet.tick()
    # never-served first (A by slug, then B), then the stamps alternate
    assert bursts(journal) == [A, B, A, B]


def test_a_products_dry_spell_never_blocks_the_other(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[B].claimable = True  # A has nothing claimable

    fleet.tick()
    fleet.tick()
    assert bursts(journal) == [B, B]  # the turn passes over the idle product


def test_a_paused_product_is_ignored_entirely(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[B].claimable = True
    stores.ledger.set_repository_state(B, ProductState.PAUSED, "op", reason="rehearsal")
    journal.clear()

    fleet.tick()
    assert all(slug == A for _, slug in journal)  # no build, no observe, no burst


def test_registration_lands_on_the_next_tick(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    fleet.tick()
    assert ("observe", B) not in journal

    register(stores, B)
    fleet.tick()
    assert ("observe", B) in journal  # no restart, no cache to invalidate


def test_a_products_trouble_stays_its_own(rig):
    """One product's unhonorable declaration or unreachable repo quarantines
    that product for the tick — the other is observed and served."""
    fleet, stores, fakes, journal, build_errors = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[B].claimable = True
    build_errors[A] = ReviewPolicyError("the reviewer App is not configured")
    journal.clear()

    fleet.tick()
    assert ("observe", B) in journal and bursts(journal) == [B]
    assert not any(slug == A for _, slug in journal)


def test_an_observe_failure_skips_the_product_not_the_fleet(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[A].observe_error = GitHubApiError("503 from GitHub")
    fakes[B].claimable = True
    journal.clear()

    fleet.tick()
    assert bursts(journal) == [B]


# --- fair share, and the pool (H1) ---


def a_burst_of(stores, slug, burst_id, *, at=None):
    """A burst that happened, as the ledger holds it — fair share's numerator is
    an aggregate over these, never a counter anyone maintains."""
    task_id = task_id_for(slug, 1)
    stores.ledger.upsert_task(TaskRecord(task_id=task_id, repository=slug, issue_number=1))
    stores.ledger.record_burst(BurstRecord(burst_id=burst_id, task_id=task_id, started_at=at or utc_now()))


def test_the_turn_follows_the_weights_the_operator_declared(rig):
    """H1's done-when: a product at twice the share is served twice as often.

    The window's history is the input, so the test writes it: with B already
    served twice and A once, A is next at equal weights — and with B's share
    doubled the ratio flips, because two bursts against a weight of 2 is the
    same claim on the fleet as one against 1.
    """
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[A].claimable = fakes[B].claimable = True
    a_burst_of(stores, B, "b-1")
    a_burst_of(stores, B, "b-2")
    a_burst_of(stores, A, "a-1")

    journal.clear()
    fleet.tick()
    assert bursts(journal) == [A]  # 1/1 beats 2/1

    stores.ledger.update_repository(B, {"usage_share": 2.0}, "op", "B carries more")
    journal.clear()
    fleet.tick()
    assert bursts(journal) == [B]  # 2/2 now beats A's 2/1 — the weight is the denominator


def test_equal_weights_and_equal_service_fall_back_to_taking_turns(rig):
    """The C4 order survives underneath: with nothing to separate two products,
    the one served least *recently* goes, so they alternate instead of one name
    always winning."""
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[A].claimable = fakes[B].claimable = True

    journal.clear()
    fleet.tick()
    first = bursts(journal)[0]
    journal.clear()
    fleet.tick()
    assert bursts(journal) == [B if first == A else A]


def test_an_exhausted_pool_ends_the_tick_without_touching_a_task(rig, monkeypatch):
    """The pool is the platform's, not a product's: an empty one is empty for
    everybody. Asking per product would claim a task, give it back, and announce
    the same exhaustion once per product — so the fleet asks once, says so once,
    and leaves every task where it was."""
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[A].claimable = fakes[B].claimable = True
    stores.ledger.register_subscription(
        declare("ada", "env:ADA_TOKEN", 1_000, "2027-09-01"), actor="op"
    )
    spent = BurstRecord(burst_id="b-spent", task_id=task_id_for(A, 1), subscription_id="ada")
    stores.ledger.upsert_task(TaskRecord(task_id=task_id_for(A, 1), repository=A, issue_number=1))
    stores.ledger.record_burst(spent)
    spent.status = BurstStatus.SUCCEEDED
    spent.finished_at = utc_now()
    spent.exit_code = 0
    spent.input_tokens = 1_000
    stores.ledger.finish_burst(spent)

    journal.clear()
    fleet.tick()
    assert bursts(journal) == []  # nothing ran
    assert ("observe", A) in journal and ("observe", B) in journal  # but the world was still read
    (event,) = [e for e in stores.ledger.events() if e.kind == "capacity_exhausted"]
    assert event.payload["products"] == 2  # once for the fleet, not once per product
    assert stores.ledger.get_repository(A).last_burst_at is None  # no turn was spent


def test_the_fleet_picks_who_pays_and_hands_it_down(rig, monkeypatch):
    fleet, stores, fakes, journal, _ = rig
    monkeypatch.setenv("ADA_TOKEN", "adas-secret")
    register(stores, A)
    stores.ledger.register_subscription(
        declare("ada", "env:ADA_TOKEN", 1_000_000, "2027-09-01"), actor="op"
    )
    fleet.tick()
    fakes[A].claimable = True

    fleet.tick()
    assert fakes[A].capacity.subscription_id == "ada"
    assert fakes[A].capacity.token == "adas-secret"


def test_the_turn_is_stamped_when_taken(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    fakes[A].claimable = True

    fleet.tick()
    assert stores.ledger.get_repository(A).last_burst_at is not None
    assert stores.ledger.get_repository(B).last_burst_at is None  # no burst, no stamp


def test_one_product_degenerates_to_the_single_loop(rig):
    """The done-when's second half: with one active product a tick is exactly
    reconcile → observe → one burst, nothing else."""
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    fleet.tick()
    fakes[A].claimable = True
    journal.clear()

    fleet.tick()
    assert journal == [("build", A), ("reconcile", A), ("observe", A), ("burst", A)]


def test_an_empty_registry_is_an_idle_tick(rig):
    fleet, _, _, journal, _ = rig
    fleet.tick()  # logs a warning, raises nothing
    assert journal == []


# --- the org STOP (E3): a row, read at the top of the tick and before the spawn ---


def test_the_org_stop_freezes_the_fleet(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    stores.ledger.stop_org("op", "drill")
    fleet.tick()
    assert journal == []  # no build, no observe, no burst — the tick is a read of the row

    stores.ledger.resume_org("op")
    fleet.tick()  # and nothing needed restarting
    assert journal == [("build", A), ("reconcile", A), ("observe", A)]


def test_a_stop_landing_during_observation_spawns_nothing(rig):
    """The second read, before the spawn: observation is GitHub round trips, and
    a `stop` that lands in the middle of them must not be followed by a burst."""
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    fleet.tick()
    fakes[A].claimable = True
    original = fakes[A].observation_turn

    def observe_then_stop():
        original()
        stores.ledger.stop_org("op", "incident")

    fakes[A].observation_turn = observe_then_stop
    journal.clear()
    fleet.tick()
    assert ("observe", A) in journal and bursts(journal) == []


def test_the_process_shutdown_flag_freezes_the_fleet_and_reaches_every_scheduler(rig):
    """SIGTERM sets the fleet's `halt` (cli._serve); the tick stands down on it
    like on the row, and every scheduler the fleet builds carries the same flag,
    so the live burst's heartbeat — whichever product owns it — fences on it."""
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    register(stores, B)
    fleet.tick()
    assert fakes[A].halt is fleet.halt and fakes[B].halt is fleet.halt
    assert stores.ledger.org_stop() is None  # a shutdown is not an org STOP: nothing written

    journal.clear()
    fleet.halt.set()
    fleet.tick()
    assert journal == []


# --- the `run` verb's fleet startup (cli seam) ---


def _run_args(tmp_path, *extra):
    return cli.build_parser().parse_args(
        ["run", "--data-dir", str(tmp_path / "data"), "--once", *extra]
    )


def _declare_app(monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("fake key material")
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key))


def test_run_without_github_needs_a_registered_product(tmp_path, monkeypatch, capsys):
    _declare_app(monkeypatch, tmp_path)
    assert cli._run_scheduler(_run_args(tmp_path)) == 2
    assert "no active products registered" in capsys.readouterr().err


def test_fleet_startup_refuses_an_unhonorable_declaration(tmp_path, monkeypatch, capsys):
    """The C3 honor-check, asked per active row before the loop starts: a
    product declaring claude on a machine without the reviewer App refuses."""
    _declare_app(monkeypatch, tmp_path)
    stores = PlatformStores.open(tmp_path / "data")
    register(stores, A, review_enabled=True, review_provider="claude")
    assert cli._run_scheduler(_run_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert A in err and "reviewer App is not configured" in err


def test_fleet_startup_refuses_e2e_without_the_tester_app(tmp_path, monkeypatch, capsys):
    # D3: the report's identity is part of e2e's honorability — same shape as
    # the claude/reviewer-App refusal above
    _declare_app(monkeypatch, tmp_path)
    monkeypatch.setenv("REVIEWER_GITHUB_APP_ID", "2")
    monkeypatch.setenv("REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "app.pem"))
    stores = PlatformStores.open(tmp_path / "data")
    register(stores, A, review_enabled=True, review_provider="claude", e2e_enabled=True)
    assert cli._run_scheduler(_run_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert A in err and "tester App is not configured" in err


def test_startup_refuses_retired_role_knobs(tmp_path, monkeypatch, capsys):
    """D5: model, budget and turns are the role row's. A machine still setting
    the env knobs they replaced would run with them silently ignored — so
    startup names each one and where it went, and refuses."""
    _declare_app(monkeypatch, tmp_path)
    stores = PlatformStores.open(tmp_path / "data")
    register(stores, A)
    monkeypatch.setenv("SCHEDULER_BURST_BUDGET_USD", "0.5")
    monkeypatch.setenv("REVIEWER_MODEL", "claude-opus-5")
    assert cli._run_scheduler(_run_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "retired settings" in err
    assert "SCHEDULER_BURST_BUDGET_USD" in err and "`role set <role> --budget`" in err
    assert "REVIEWER_MODEL" in err and "`role set reviewer --model`" in err
