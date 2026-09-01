"""The registry verbs, driven the way an operator drives them (C1, #23).

Through ``build_parser`` + the ``_product`` handler rather than ``main()``:
``main`` loads the deployment's own .env, and these tests declare their platform
defaults explicitly instead. The store-level contract (audit-first mutations,
CAS transitions, both backends) is `test_ledger_contract.py`'s; this file tests
the operator surface — materialized defaults, flag overrides, refusals that
never become rows.
"""

import pytest
from control_plane import cli
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
    ):
        monkeypatch.delenv(name, raising=False)


def run(tmp_path, *argv):
    args = cli.build_parser().parse_args(
        ["product", *argv, "--data-dir", str(tmp_path), "--actor", "op"]
    )
    return cli._product(args)


def stored(tmp_path, repository):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        return ledger.get_repository(repository)
    finally:
        ledger.close()


def test_register_materializes_the_platform_defaults(tmp_path, monkeypatch):
    """TO-BE §2: `product register` fills every policy/quota column from
    .env-shaped platform defaults — the row is the entire truth."""
    monkeypatch.setenv("REVIEW", "on")
    monkeypatch.setenv("REVIEW_PROVIDERS", "claude")
    monkeypatch.setenv("E2E", "on")
    monkeypatch.setenv("SCHEDULER_DAILY_BURST_CEILING", "7")

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


def test_set_changes_only_what_was_named(tmp_path):
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
