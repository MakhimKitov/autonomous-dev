"""`github-check` preflights every configured pipeline role (D1, TO-BE §4)."""

import pytest
from control_plane import cli
from control_plane.github_auth import ROLE_PREFIXES, GitHubAuthError, InstallationToken

ROLE_ENV_NAMES = [
    prefix + name
    for prefix in ROLE_PREFIXES.values()
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_INSTALLATION_ID")
]


@pytest.fixture(autouse=True)
def hermetic_app_env(monkeypatch):
    """The operator's shell configures real Apps — tests must not see them."""
    for name in ROLE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _declare(monkeypatch, tmp_path, prefix, app_id):
    key = tmp_path / f"{prefix or 'dev_'}key.pem"
    key.write_text("not-a-real-key", encoding="utf-8")
    monkeypatch.setenv(f"{prefix}GITHUB_APP_ID", app_id)
    monkeypatch.setenv(f"{prefix}GITHUB_APP_PRIVATE_KEY_PATH", str(key))


def _token(slug, permissions=None):
    return InstallationToken(
        token=f"ghs_{slug}{'0' * 24}",
        expires_at="2026-09-03T12:00:00Z",
        permissions=permissions or {"contents": "write", "issues": "write"},
        app_slug=slug,
    )


def test_nothing_configured_refuses(capsys):
    assert cli._github_check() == 2
    out = capsys.readouterr().out
    assert "no App configured" in out
    # every role got its not-configured line, prefixed names spelled out
    for role, prefix in ROLE_PREFIXES.items():
        assert f"{role}: not configured ({prefix}GITHUB_APP_ID" in out


def test_every_configured_role_preflights(tmp_path, monkeypatch, capsys):
    # done-when (D1): all configured roles preflight — one check, three Apps
    _declare(monkeypatch, tmp_path, "", "1")
    _declare(monkeypatch, tmp_path, "REVIEWER_", "2")
    _declare(monkeypatch, tmp_path, "TESTER_", "3")
    slugs = {"1": "auto-dev", "2": "auto-reviewer", "3": "auto-tester"}
    monkeypatch.setattr(cli, "mint_installation_token", lambda creds: _token(slugs[creds.app_id]))
    assert cli._github_check() == 0
    out = capsys.readouterr().out
    for slug in slugs.values():
        assert f"app:          {slug}" in out
        assert f"{slug}[bot]" in out
    assert "monitor: not configured (MONITOR_GITHUB_APP_ID" in out


def test_a_configured_role_that_cannot_mint_fails_the_check(tmp_path, monkeypatch, capsys):
    _declare(monkeypatch, tmp_path, "", "1")
    _declare(monkeypatch, tmp_path, "TESTER_", "3")

    def mint(creds):
        if creds.app_id == "3":
            raise GitHubAuthError("the App has no installations — install it on the product repo")
        return _token("auto-dev")

    monkeypatch.setattr(cli, "mint_installation_token", mint)
    assert cli._github_check() == 1
    captured = capsys.readouterr()
    assert "tester: error: the App has no installations" in captured.err
    assert "roles failed the check: tester" in captured.err
    assert "app:          auto-dev" in captured.out  # the healthy role still reports


def test_a_half_declared_role_is_an_error_not_a_skip(tmp_path, monkeypatch, capsys):
    _declare(monkeypatch, tmp_path, "", "1")
    monkeypatch.setenv("TESTER_GITHUB_APP_ID", "3")  # no key path
    monkeypatch.setattr(cli, "mint_installation_token", lambda creds: _token("auto-dev"))
    assert cli._github_check() == 1
    assert "tester: error:" in capsys.readouterr().err


def test_unexpected_permissions_warn_per_role(tmp_path, monkeypatch, capsys):
    _declare(monkeypatch, tmp_path, "", "1")
    monkeypatch.setattr(
        cli,
        "mint_installation_token",
        lambda creds: _token("auto-dev", {"contents": "write", "administration": "write"}),
    )
    assert cli._github_check() == 0
    assert "⚠ unexpected permissions present" in capsys.readouterr().out
