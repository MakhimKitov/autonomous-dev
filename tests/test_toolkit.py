"""The image-baked toolkit plugin: structure, skill content, burst wiring."""

import json
from pathlib import Path

from dev_agent import burst
from dev_agent.env import BurstEnv

TOOLKIT = Path(__file__).parent.parent / "docker" / "dev-agent" / "toolkit"


def test_plugin_manifest_is_valid():
    manifest = json.loads((TOOLKIT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "dev-agent-toolkit"


def test_no_codex_skill_in_the_dev_toolkit():
    """REVIEWERS.md slice 2: the pre-PR self-review subsystem left the dev
    burst — codex returns as a reviewer-burst engine (slice 3), not a skill
    the dev invokes. The binaries and walls stay in the image."""
    assert not (TOOLKIT / "skills" / "codex-review").exists()


def test_codex_shim_enforces_the_walls():
    """Issue #1: scrub + refusal live in the binary the agent can't edit."""
    shim = (TOOLKIT.parent / "codex-shim.sh").read_text(encoding="utf-8")
    assert shim.startswith("#!/bin/sh")
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        assert f"-u {var}" in shim  # unconditional credential scrub
    assert "--dangerously-bypass-approvals-and-sandbox" in shim
    assert "*danger-full-access*" in shim
    assert "--yolo" in shim
    assert "exit 64" in shim  # refused, not silently rewritten
    assert "codex-real" in shim


def test_burst_loads_toolkit_only_when_present(monkeypatch, tmp_path):
    env = BurstEnv(task_id="1", burst_id="b1", repo_dir=tmp_path)

    monkeypatch.setattr(burst, "TOOLKIT_PLUGIN_PATH", tmp_path / "missing")
    assert burst._to_options(env).plugins == []

    monkeypatch.setattr(burst, "TOOLKIT_PLUGIN_PATH", TOOLKIT)
    plugins = burst._to_options(env).plugins
    assert plugins == [{"type": "local", "path": str(TOOLKIT)}]
