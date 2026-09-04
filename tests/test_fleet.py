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
from control_plane.models import ProductState, RepositoryRecord, TaskRecord, task_id_for
from control_plane.review import ReviewPolicyError
from control_plane.scheduler import Fleet

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
        self.current_burst_id = None

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

    def run_task_burst(self, task):
        self.journal.append(("burst", self.slug))


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


def test_the_stop_flag_freezes_the_fleet(rig):
    fleet, stores, fakes, journal, _ = rig
    register(stores, A)
    fleet.stop_path.touch()
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
