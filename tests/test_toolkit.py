"""The image-baked toolkit plugin: structure, skill content, burst wiring."""

import json
from pathlib import Path

from dev_agent import burst
from dev_agent.env import BurstEnv

TOOLKIT = Path(__file__).parent.parent / "docker" / "dev-agent" / "toolkit"


def test_plugin_manifest_is_valid():
    manifest = json.loads((TOOLKIT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "dev-agent-toolkit"


def test_codex_review_skill_pins_the_safe_invocation():
    skill = (TOOLKIT / "skills" / "codex-review" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---")
    assert "name: codex-review" in skill
    assert "env -u GH_TOKEN" in skill  # credential scrub
    assert 'sandbox_mode="read-only"' in skill
    assert "--dangerously-bypass-approvals-and-sandbox" in skill  # named as forbidden
    assert "two rounds" in skill


def test_burst_loads_toolkit_only_when_present(monkeypatch, tmp_path):
    env = BurstEnv(task_id="1", burst_id="b1", repo_dir=tmp_path)

    monkeypatch.setattr(burst, "TOOLKIT_PLUGIN_PATH", tmp_path / "missing")
    assert burst._to_options(env).plugins == []

    monkeypatch.setattr(burst, "TOOLKIT_PLUGIN_PATH", TOOLKIT)
    plugins = burst._to_options(env).plugins
    assert plugins == [{"type": "local", "path": str(TOOLKIT)}]
