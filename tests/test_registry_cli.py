"""The registry verbs, driven the way an operator drives them (C1 #23, C2 #24).

Through ``build_parser`` + the ``_product`` handler rather than ``main()``:
``main`` loads the deployment's own .env, and these tests declare their platform
defaults explicitly instead. The store-level contract (audit-first mutations,
CAS transitions, both backends) is `test_ledger_contract.py`'s; the preflight
boundary itself is `test_preflight.py`'s. This file tests the operator surface —
materialized defaults, flag overrides, refusals that never become rows, and the
C2 wiring: the gate runs before the write, the lint lands in the audit row, and
``product check`` re-runs it all against the live repo.
"""

import pytest
from control_plane import cli
from control_plane.github_api import GitHubApiError
from control_plane.preflight import PreflightError
from control_plane.store import SqliteLedger


@pytest.fixture(autouse=True)
def hermetic_platform_env(monkeypatch):
    """The verbs materialize from the platform env — pin it to 'nothing declared'
    so a developer's real .env-shaped exports can't steer a test."""
    for name in (
        "REVIEW",
        "REVIEW_PROVIDERS",
        "REVIEWER_MODEL",
        "E2E",
        "TESTER_MODEL",
        "SCHEDULER_DAILY_BURST_CEILING",
        "GITHUB_COPILOT_REVIEW_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "REVIEWER_GITHUB_APP_ID",
        "REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH",
        "TEST_DISCORD_TOKEN",
        "TEST_GUILD_ID",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeProductRepo:
    """The org App's eye on a fake product repo — the preflight seam."""

    def __init__(self, files=None, reachable=True):
        self.files = dict(files or {})
        self.reachable = reachable
        self.filed = []

    def repo_owner(self):
        if not self.reachable:
            raise GitHubApiError("reading the repo: HTTP 404 Not Found")
        return "owner"

    def read_file(self, path):
        return self.files.get(path)

    def path_exists(self, path):
        prefix = path.rstrip("/") + "/"
        return path in self.files or any(name.startswith(prefix) for name in self.files)

    def create_issue(self, title, body, labels):
        self.filed.append({"title": title, "body": body, "labels": labels})
        return 7


# a manifest that declares no env keeps C1's register calls satisfiable as-is;
# tests about coverage/refs bring their own manifest
FULL_SOCKET = {
    "TESTING.md": "---\nenv: []\ntools: []\n---\n\nBoot it and poke it.",
    "CLAUDE.md": "the product's operating contract",
    ".github/ISSUE_TEMPLATE/issue.yml": "form",
    ".github/PULL_REQUEST_TEMPLATE.md": "template",
    ".commitlintrc.json": "{}",
}


def run(tmp_path, *argv, api=None):
    args = cli.build_parser().parse_args(
        ["product", *argv, "--data-dir", str(tmp_path), "--actor", "op"]
    )
    repo = FakeProductRepo(FULL_SOCKET) if api is None else api
    return cli._product(args, api_factory=lambda slug: repo)


def stored(tmp_path, repository):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        return ledger.get_repository(repository)
    finally:
        ledger.close()


def audit(tmp_path, repository):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        return ledger.audit_trail(repository)
    finally:
        ledger.close()


def declare_reviewer_creds(monkeypatch):
    """What `from_env` wants to see for the claude provider — fake values;
    preflight checks presence, never validity (that stays the App exchange's)."""
    monkeypatch.setenv("REVIEWER_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH", "/keys/reviewer.pem")


def test_register_materializes_the_platform_defaults(tmp_path, monkeypatch):
    """TO-BE §2: `product register` fills every policy/quota column from
    .env-shaped platform defaults — the row is the entire truth."""
    monkeypatch.setenv("REVIEW", "on")
    monkeypatch.setenv("REVIEW_PROVIDERS", "claude")
    monkeypatch.setenv("E2E", "on")
    monkeypatch.setenv("SCHEDULER_DAILY_BURST_CEILING", "7")
    declare_reviewer_creds(monkeypatch)

    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0

    row = stored(tmp_path, "owner/product")
    assert row.state == "active"
    assert row.team == "bots"
    assert row.review_enabled is True and row.review_provider == "claude"
    assert row.e2e_enabled is True
    assert (row.concurrency, row.daily_bursts, row.usage_share) == (1, 7, 1.0)
    assert row.test_env_refs == {}


def test_flags_override_the_env_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW", "on")
    monkeypatch.setenv("REVIEW_PROVIDERS", "claude")
    monkeypatch.setenv("E2E", "on")

    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--review", "off", "--e2e", "off",
        "--concurrency", "2", "--daily-bursts", "3", "--usage-share", "0.5",
    ) == 0

    row = stored(tmp_path, "owner/product")
    # the toggle turned off; the curated selector survives it, as in the env world
    assert row.review_enabled is False and row.review_provider == "claude"
    assert row.e2e_enabled is False
    assert (row.concurrency, row.daily_bursts, row.usage_share) == (2, 3, 0.5)


def test_an_unhonorable_declaration_never_becomes_a_row(tmp_path, monkeypatch, capsys):
    """REVIEW=on with no providers refuses to start the scheduler; registration
    routes through the same validator, so it must not become a row either."""
    monkeypatch.setenv("REVIEW", "on")

    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 2

    assert stored(tmp_path, "owner/product") is None
    assert "REVIEW_PROVIDERS" in capsys.readouterr().err


def test_test_env_takes_refs_never_values(tmp_path, capsys):
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--test-env", "DISCORD_TOKEN=env:TEST_DISCORD_TOKEN",
        "--test-env", "GUILD_ID=file:/run/secrets/guild",
    ) == 0
    assert stored(tmp_path, "owner/product").test_env_refs == {
        "DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN",
        "GUILD_ID": "file:/run/secrets/guild",
    }

    assert run(
        tmp_path, "register", "owner/other", "--team", "bots",
        "--test-env", "TOKEN=hunter2-an-actual-secret",
    ) == 2
    assert stored(tmp_path, "owner/other") is None
    assert "never a value" in capsys.readouterr().err


def test_registering_twice_is_a_refusal_not_a_crash(tmp_path, capsys):
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    assert run(tmp_path, "register", "owner/product", "--team", "other") == 2
    assert "already registered" in capsys.readouterr().err
    assert stored(tmp_path, "owner/product").team == "bots"  # the first declaration stands


def test_a_product_is_owner_slash_name(tmp_path, capsys):
    assert run(tmp_path, "register", "not-a-slug", "--team", "bots") == 2
    assert "owner/name" in capsys.readouterr().err


def test_quotas_must_make_sense(tmp_path, capsys):
    assert run(tmp_path, "register", "owner/product", "--team", "bots", "--concurrency", "0") == 2
    assert "at least 1" in capsys.readouterr().err


def test_pause_requires_a_reason_at_the_parser(tmp_path, capsys):
    """The reason is not an optional nicety — argparse itself refuses (TO-BE §2)."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["product", "pause", "owner/product"])
    assert "--reason" in capsys.readouterr().err


def test_pause_resume_and_show_carry_the_trail(tmp_path, capsys):
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    assert run(tmp_path, "pause", "owner/product", "--reason", "incident 12") == 0
    assert stored(tmp_path, "owner/product").state == "paused"
    assert run(tmp_path, "resume", "owner/product") == 0
    assert stored(tmp_path, "owner/product").state == "active"

    capsys.readouterr()
    assert run(tmp_path, "show", "owner/product") == 0
    out = capsys.readouterr().out
    assert "product.register" in out
    assert "product.pause — incident 12" in out
    assert "product.resume" in out


def test_set_changes_only_what_was_named(tmp_path, monkeypatch):
    declare_reviewer_creds(monkeypatch)
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    before = stored(tmp_path, "owner/product")

    assert run(
        tmp_path, "set", "owner/product",
        "--review", "on", "--review-providers", "claude",
        "--reason", "review goes live",
    ) == 0

    after = stored(tmp_path, "owner/product")
    assert after.review_enabled is True and after.review_provider == "claude"
    # everything unnamed is untouched — set is surgical, never a re-materialization
    assert (after.team, after.e2e_enabled, after.concurrency, after.daily_bursts) == (
        before.team, before.e2e_enabled, before.concurrency, before.daily_bursts,
    )


def test_set_with_nothing_to_set_is_a_refusal(tmp_path, capsys):
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    assert run(tmp_path, "set", "owner/product") == 2
    assert "nothing to set" in capsys.readouterr().err


def test_set_review_on_still_needs_a_provider(tmp_path, capsys):
    """`set` validates the *resulting* row through the same policy validator:
    flipping review on with no selector anywhere is refused, not stored."""
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    assert run(tmp_path, "set", "owner/product", "--review", "on") == 2
    assert stored(tmp_path, "owner/product").review_enabled is False
    assert "REVIEW_PROVIDERS" in capsys.readouterr().err


def test_set_never_reads_the_platform_env(tmp_path, monkeypatch):
    """A changed platform default never retro-propagates: `set --review on` on a
    row that already carries a selector uses the row, not $REVIEW_PROVIDERS."""
    declare_reviewer_creds(monkeypatch)
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--review", "on", "--review-providers", "claude",
    ) == 0
    assert run(tmp_path, "set", "owner/product", "--review", "off") == 0

    monkeypatch.setenv("REVIEW_PROVIDERS", "copilot")  # must be ignored
    assert run(tmp_path, "set", "owner/product", "--review", "on") == 0
    assert stored(tmp_path, "owner/product").review_provider == "claude"


def test_list_lists_everything_registered(tmp_path, capsys):
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    assert run(tmp_path, "register", "owner/other", "--team", "web") == 0
    capsys.readouterr()
    assert run(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert out.index("owner/other") < out.index("owner/product")  # one order: by name
    assert "team=bots" in out and "team=web" in out


def test_show_of_the_unregistered_says_so(tmp_path, capsys):
    assert run(tmp_path, "show", "ghost/none") == 2
    assert "not registered" in capsys.readouterr().err


# --- registration preflight (C2, #24) ---


def test_a_false_declaration_is_refused(tmp_path, monkeypatch, capsys):
    """C2's done-when, half one: `--e2e on` against a repo with no TESTING.md
    is a declaration the platform cannot execute — refused, never a row."""
    declare_reviewer_creds(monkeypatch)
    files = {name: text for name, text in FULL_SOCKET.items() if name != "TESTING.md"}

    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--review", "on", "--review-providers", "claude", "--e2e", "on",
        api=FakeProductRepo(files),
    ) == 2

    assert "ships no TESTING.md" in capsys.readouterr().err
    assert stored(tmp_path, "owner/product") is None
    assert audit(tmp_path, "owner/product") == []  # a refusal leaves no trail


def test_a_broken_socket_registers_with_warnings(tmp_path, capsys):
    """C2's done-when, half two: socket gaps warn — on the terminal and into
    the registration's audit row — and the product still registers."""
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--reason", "onboarding",
        api=FakeProductRepo(files={}),
    ) == 0

    out = capsys.readouterr().out
    assert "socket gap" in out and "⚠" in out
    row = stored(tmp_path, "owner/product")
    assert row is not None and row.state == "active"
    (entry,) = audit(tmp_path, "owner/product")
    assert entry.reason.startswith("onboarding [lint: ")
    assert "CLAUDE.md" in entry.reason and "commitlint" in entry.reason


def test_an_unreadable_repo_never_becomes_a_row(tmp_path, capsys):
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        api=FakeProductRepo(reachable=False),
    ) == 2
    assert "cannot read owner/product" in capsys.readouterr().err
    assert stored(tmp_path, "owner/product") is None


def test_a_provider_needs_its_credentials_to_register(tmp_path, capsys):
    """The scheduler-startup error, relocated: no reviewer App on this machine
    means a claude-declaring product cannot be registered from it."""
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--review", "on", "--review-providers", "claude",
    ) == 2
    assert "reviewer App is not configured" in capsys.readouterr().err
    assert stored(tmp_path, "owner/product") is None


def test_set_asks_the_gate_for_policy_columns(tmp_path, monkeypatch, capsys):
    """`set` can shape the same false declaration register refuses — flipping
    e2e on without a satisfiable manifest is refused, and the row stands."""
    declare_reviewer_creds(monkeypatch)
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--review", "on", "--review-providers", "claude",
    ) == 0

    broken = FakeProductRepo({name: text for name, text in FULL_SOCKET.items() if name != "TESTING.md"})
    assert run(tmp_path, "set", "owner/product", "--e2e", "on", api=broken) == 2
    assert "ships no TESTING.md" in capsys.readouterr().err
    assert stored(tmp_path, "owner/product").e2e_enabled is False

    assert run(tmp_path, "set", "owner/product", "--e2e", "on") == 0
    assert stored(tmp_path, "owner/product").e2e_enabled is True


def test_quota_set_stays_offline(tmp_path):
    """Quotas and team are the platform's own business — changing them never
    probes GitHub, so they stay settable from a machine with no App at all."""

    class NeverCalled:
        def __getattr__(self, name):  # any probe is a failure
            raise AssertionError("preflight probed GitHub for a quota change")

    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0
    assert run(tmp_path, "set", "owner/product", "--concurrency", "3", api=NeverCalled()) == 0
    assert run(tmp_path, "pause", "owner/product", "--reason", "hold", api=NeverCalled()) == 0
    assert stored(tmp_path, "owner/product").concurrency == 3


def test_check_reruns_preflight_and_catches_drift(tmp_path, monkeypatch, capsys):
    """`product check` (TO-BE §2): the same preflight, on demand. A socket
    that rotted after registration flips the exit code, not the row."""
    declare_reviewer_creds(monkeypatch)
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "fake-token")
    manifest = "---\nenv: [DISCORD_TOKEN]\n---\nprose"
    healthy = FakeProductRepo(dict(FULL_SOCKET, **{"TESTING.md": manifest}))
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots",
        "--review", "on", "--review-providers", "claude", "--e2e", "on",
        "--test-env", "DISCORD_TOKEN=env:TEST_DISCORD_TOKEN",
        api=healthy,
    ) == 0

    capsys.readouterr()
    assert run(tmp_path, "check", "owner/product", api=healthy) == 0
    assert "socket clean" in capsys.readouterr().out

    drifted = FakeProductRepo({name: text for name, text in healthy.files.items() if name != "TESTING.md"})
    assert run(tmp_path, "check", "owner/product", api=drifted) == 1
    assert "NOT operable" in capsys.readouterr().err
    assert stored(tmp_path, "owner/product").e2e_enabled is True  # check never mutates


def test_check_of_the_unregistered_says_so(tmp_path, capsys):
    assert run(tmp_path, "check", "ghost/none") == 2
    assert "not registered" in capsys.readouterr().err


def test_lint_issue_files_the_gaps_as_human(tmp_path, capsys):
    """`--lint-issue` (TO-BE §2): the gaps become the product's own backlog,
    filed with `human` provenance on the operator's explicit flag."""
    gappy = FakeProductRepo({"CLAUDE.md": "contract"})
    assert run(tmp_path, "register", "owner/product", "--team", "bots") == 0

    capsys.readouterr()
    assert run(tmp_path, "check", "owner/product", "--lint-issue", api=gappy) == 0
    assert "lint issue filed: owner/product#7" in capsys.readouterr().out
    (issue,) = gappy.filed
    assert issue["labels"] == ["human"]
    assert "TESTING.md" in issue["body"] and "PR template" in issue["body"]
    assert "CLAUDE.md" not in issue["body"]  # only the gaps, not the whole checklist


def test_lint_issue_with_a_clean_socket_files_nothing(tmp_path, capsys):
    clean = FakeProductRepo(FULL_SOCKET)
    assert run(
        tmp_path, "register", "owner/product", "--team", "bots", "--lint-issue", api=clean
    ) == 0
    assert clean.filed == []
    assert "no lint issue to file" in capsys.readouterr().out


def test_preflight_needs_the_app(monkeypatch):
    """The real factory: a machine with no GitHub App cannot vouch that the App
    reads the repo — registration refuses rather than guesses."""
    with pytest.raises(PreflightError, match="GITHUB_APP_ID"):
        cli._preflight_api("owner/product")
