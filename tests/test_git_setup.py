"""Pre-burst git setup: App-bot identity + gh credential helper, local config only."""

import subprocess

from dev_agent.burst import setup_git
from dev_agent.env import BurstEnv


def _clone(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _env(tmp_path, **overrides):
    defaults = dict(task_id="42", burst_id="b1", repo_dir=_clone(tmp_path))
    return BurstEnv(**(defaults | overrides))


def test_configures_identity_and_helper(tmp_path):
    env = _env(
        tmp_path,
        github_token="ghs_x",
        git_user_name="auto-dev[bot]",
        git_user_email="1+auto-dev[bot]@users.noreply.github.com",
    )
    setup_git(env)
    config = (env.repo_dir / ".git" / "config").read_text(encoding="utf-8")
    assert "auto-dev[bot]" in config
    assert "gh auth git-credential" in config
    assert "ghs_x" not in config  # the token never touches disk
    # SSH origins must be rewritten — the container has no ssh (no keys, by design)
    assert 'insteadOf = git@github.com:' in config


def test_noop_without_token(tmp_path):
    env = _env(tmp_path)
    setup_git(env)
    config = (env.repo_dir / ".git" / "config").read_text(encoding="utf-8")
    assert "credential" not in config
