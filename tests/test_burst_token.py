"""The burst token tier (D2): fresh per spawn, downscoped below the App's grant."""

import pytest
from control_plane import bursts
from control_plane.github_auth import BURST_PERMISSIONS, GitHubAuthError, InstallationToken
from control_plane.models import TaskRecord


@pytest.fixture(autouse=True)
def hermetic_app_env(monkeypatch):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_INSTALLATION_ID"):
        monkeypatch.delenv(name, raising=False)


def _declare_app(monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("not-a-real-key", encoding="utf-8")
    monkeypatch.setenv("GITHUB_APP_ID", "42")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key))


def _task(slug="MakhimKitov/discord-bot"):
    return TaskRecord(task_id=f"{slug}#7", repository=slug, issue_number=7)


def test_the_dev_burst_token_pins_repo_and_permissions(tmp_path, monkeypatch):
    _declare_app(monkeypatch, tmp_path)
    captured = {}

    def fake_mint(creds, repositories=None, permissions=None, **kwargs):
        captured["repositories"] = repositories
        captured["permissions"] = permissions
        return InstallationToken(
            token="ghs_scoped", expires_at="", permissions=permissions or {}, app_slug="auto-dev"
        )

    monkeypatch.setattr(bursts, "mint_installation_token", fake_mint)
    token, name, email = bursts.mint_burst_token(_task(), "dev")
    # the API takes bare repo names, resolved within the installation
    assert captured["repositories"] == ["discord-bot"]
    assert captured["permissions"] == BURST_PERMISSIONS["dev"]
    assert token == "ghs_scoped" and name == "auto-dev[bot]"
    assert email == "42+auto-dev[bot]@users.noreply.github.com"


def test_a_credential_free_role_cannot_mint(tmp_path, monkeypatch):
    # reviewer/tester bursts hold no GitHub credential at all — asking for one
    # is a broken code path, refused loudly, never a broad token by accident
    _declare_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bursts, "mint_installation_token", lambda *a, **k: pytest.fail("must not mint")
    )
    for role in ("reviewer", "tester"):
        with pytest.raises(GitHubAuthError, match="holds no GitHub credential"):
            bursts.mint_burst_token(_task(), role)


def test_unconfigured_app_means_no_github(monkeypatch):
    monkeypatch.setattr(
        bursts, "mint_installation_token", lambda *a, **k: pytest.fail("must not mint")
    )
    assert bursts.mint_burst_token(_task(), "dev") is None
