"""Registration preflight (C2, #24): the operability gate + socket lint.

The boundary under test is TO-BE §2's: a declaration the platform cannot
execute is refused; everything else the socket lacks is a warning. The repo is
a dict of files behind the ``ProductRepo`` protocol — the CLI wiring (where the
gate refuses registration and the lint lands in the audit row) is
``test_registry_cli.py``'s.
"""

import pytest
from control_plane.github_api import GitHubApiError
from control_plane.models import RepositoryRecord
from control_plane.preflight import PreflightError, run_preflight


class FakeRepo:
    """A product repo as preflight sees one: reachable (or not) and a file dict."""

    def __init__(self, files=None, reachable=True):
        self.files = dict(files or {})
        self.reachable = reachable

    def repo_owner(self):
        if not self.reachable:
            raise GitHubApiError("reading the repo: HTTP 404 Not Found")
        return "owner"

    def read_file(self, path):
        return self.files.get(path)

    def path_exists(self, path):
        prefix = path.rstrip("/") + "/"
        return path in self.files or any(name.startswith(prefix) for name in self.files)


MANIFEST = "---\nenv: [DISCORD_TOKEN]\ntools: []\n---\n\nBoot it and poke it."

FULL_SOCKET = {
    "TESTING.md": MANIFEST,
    "CLAUDE.md": "the product's operating contract",
    ".github/ISSUE_TEMPLATE/issue.yml": "form",
    ".github/PULL_REQUEST_TEMPLATE.md": "template",
    ".commitlintrc.json": "{}",
}

# what ReviewPolicy.from_env wants to see for the shipped providers
CREDS = {
    "GITHUB_COPILOT_REVIEW_TOKEN": "github_pat_fake",
    "REVIEWER_GITHUB_APP_ID": "12345",
    "REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH": "/keys/reviewer.pem",
}


def product(**overrides) -> RepositoryRecord:
    fields = {"repository": "owner/product", "team": "bots"}
    fields.update(overrides)
    return RepositoryRecord(**fields)


# --- the gate: refusals the platform cannot execute around ---


def test_an_unreachable_repo_refuses():
    """Everything preflight knows it learns through the App's token — a repo
    the App cannot read gates first, whatever else is declared."""
    with pytest.raises(PreflightError, match="cannot read owner/product"):
        run_preflight(product(), FakeRepo(FULL_SOCKET, reachable=False), environ={})


def test_a_provider_without_its_credentials_refuses():
    """The scheduler-startup error, relocated to registration (TO-BE §2)."""
    row = product(review_enabled=True, review_provider="claude")
    with pytest.raises(PreflightError, match="reviewer App is not configured"):
        run_preflight(row, FakeRepo(FULL_SOCKET), environ={})


def test_copilot_without_its_token_refuses():
    row = product(review_enabled=True, review_provider="copilot")
    with pytest.raises(PreflightError, match="GITHUB_COPILOT_REVIEW_TOKEN"):
        run_preflight(row, FakeRepo(FULL_SOCKET), environ={})


def test_credentialed_providers_pass():
    row = product(review_enabled=True, review_provider="claude,copilot")
    assert run_preflight(row, FakeRepo(FULL_SOCKET), environ=dict(CREDS)) == []


def test_e2e_needs_the_claude_provider():
    """The funnel (TESTER.md): tests trigger on the claude provider's APPROVE —
    e2e without it declares a stage that can never run."""
    row = product(e2e_enabled=True, test_env_refs={"DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN"})
    with pytest.raises(PreflightError, match="claude review provider"):
        run_preflight(row, FakeRepo(FULL_SOCKET), environ=dict(CREDS))


def _e2e_row(**overrides) -> RepositoryRecord:
    fields = {
        "review_enabled": True,
        "review_provider": "claude",
        "e2e_enabled": True,
        "test_env_refs": {"DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN"},
    }
    fields.update(overrides)
    return product(**fields)


E2E_ENVIRON = dict(CREDS, TEST_DISCORD_TOKEN="fake-token")


def test_e2e_without_a_manifest_refuses():
    files = {name: text for name, text in FULL_SOCKET.items() if name != "TESTING.md"}
    with pytest.raises(PreflightError, match="ships no TESTING.md"):
        run_preflight(_e2e_row(), FakeRepo(files), environ=E2E_ENVIRON)


def test_e2e_with_an_unparseable_manifest_refuses():
    files = dict(FULL_SOCKET, **{"TESTING.md": "no frontmatter here"})
    with pytest.raises(PreflightError, match="frontmatter"):
        run_preflight(_e2e_row(), FakeRepo(files), environ=E2E_ENVIRON)


def test_e2e_with_uncovered_manifest_env_refuses():
    files = dict(FULL_SOCKET, **{"TESTING.md": "---\nenv: [DISCORD_TOKEN, GUILD_ID]\n---\nprose"})
    with pytest.raises(PreflightError, match="GUILD_ID"):
        run_preflight(_e2e_row(), FakeRepo(files), environ=E2E_ENVIRON)


def test_e2e_with_an_unresolvable_env_ref_refuses():
    """The ref must point at something — the declaration is executable, or it
    is refused. The value itself is never read (D4 resolves)."""
    with pytest.raises(PreflightError, match="resolve to nothing"):
        run_preflight(_e2e_row(), FakeRepo(FULL_SOCKET), environ=dict(CREDS))


def test_a_file_ref_resolves_against_the_filesystem(tmp_path):
    key = tmp_path / "discord-token"
    row = _e2e_row(test_env_refs={"DISCORD_TOKEN": f"file:{key}"})
    with pytest.raises(PreflightError, match="resolve to nothing"):
        run_preflight(row, FakeRepo(FULL_SOCKET), environ=dict(CREDS))
    key.write_text("fake-token")
    assert run_preflight(row, FakeRepo(FULL_SOCKET), environ=dict(CREDS)) == []


def test_a_satisfiable_e2e_declaration_passes():
    assert run_preflight(_e2e_row(), FakeRepo(FULL_SOCKET), environ=E2E_ENVIRON) == []


# --- the lint: warnings, never refusals ---


def test_a_broken_socket_warns_never_refuses():
    """TO-BE §2: a socket-poor repo registers and works conservatively — every
    gap is a warning with a consequence, and none of them gates."""
    warnings = run_preflight(product(), FakeRepo(files={}), environ={})
    text = " / ".join(warnings)
    assert len(warnings) == 5
    assert "TESTING.md" in text
    assert "CLAUDE.md" in text
    assert "issue forms" in text
    assert "PR template" in text
    assert "commitlint" in text


def test_the_full_socket_is_silent():
    assert run_preflight(product(), FakeRepo(FULL_SOCKET), environ={}) == []


def test_an_unusable_manifest_is_lint_while_e2e_is_off():
    """The same fact moves sides of the boundary with the toggle: an
    unparseable TESTING.md gates `e2e on` and only warns while it is off."""
    files = dict(FULL_SOCKET, **{"TESTING.md": "no frontmatter here"})
    warnings = run_preflight(product(), FakeRepo(files), environ={})
    assert len(warnings) == 1
    assert "unusable TESTING.md" in warnings[0] and "--e2e on" in warnings[0]


def test_stale_test_env_refs_warn():
    """Refs the manifest never asks for are drift, not danger — the injection
    intersection already keeps them out of any container."""
    row = product(test_env_refs={"GUILD_ID": "env:TEST_GUILD_ID"})
    warnings = run_preflight(row, FakeRepo(FULL_SOCKET), environ={})
    assert len(warnings) == 1
    assert "never asks for: GUILD_ID" in warnings[0]
