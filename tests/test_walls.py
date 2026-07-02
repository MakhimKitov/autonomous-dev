"""The in-container walls: deny logic + violations feed."""

import asyncio
import json

from dev_agent.walls import forbidden_reason, make_pre_tool_use_hook


def test_private_dir_denied(tmp_path):
    assert forbidden_reason("Read", {"file_path": "private/policy.md"}, tmp_path)
    assert forbidden_reason("Write", {"file_path": str(tmp_path / "private" / "x")}, tmp_path)
    assert forbidden_reason("Grep", {"path": "src/private/nested.txt"}, tmp_path)


def test_env_files_denied_but_example_allowed(tmp_path):
    assert forbidden_reason("Read", {"file_path": ".env"}, tmp_path)
    assert forbidden_reason("Read", {"file_path": "conf/.env.production"}, tmp_path)
    assert forbidden_reason("Read", {"file_path": ".env.example"}, tmp_path) is None


def test_key_material_denied(tmp_path):
    assert forbidden_reason("Read", {"file_path": "deploy/app.pem"}, tmp_path)
    assert forbidden_reason("Read", {"file_path": "certs/host.key"}, tmp_path)


def test_escape_outside_repo_denied(tmp_path):
    assert forbidden_reason("Read", {"file_path": "../outside.txt"}, tmp_path)
    assert forbidden_reason("Read", {"file_path": "/etc/passwd"}, tmp_path)


def test_normal_paths_allowed(tmp_path):
    assert forbidden_reason("Read", {"file_path": "src/main.py"}, tmp_path) is None
    assert forbidden_reason("Edit", {"file_path": str(tmp_path / "README.md")}, tmp_path) is None


def test_bash_screen(tmp_path):
    assert forbidden_reason("Bash", {"command": "cat private/creds"}, tmp_path)
    assert forbidden_reason("Bash", {"command": "cat .env"}, tmp_path)
    assert forbidden_reason("Bash", {"command": "cp .env.example .env.example.bak"}, tmp_path) is None
    assert forbidden_reason("Bash", {"command": "pytest -q"}, tmp_path) is None


def test_hook_denies_and_records(tmp_path):
    violations = tmp_path / "violations.jsonl"
    hook = make_pre_tool_use_hook(tmp_path, violations)
    out = asyncio.run(
        hook({"tool_name": "Read", "tool_input": {"file_path": "private/x"}}, "t1", None)
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("WALL:")
    entry = json.loads(violations.read_text().splitlines()[0])
    assert entry["tool"] == "Read"

    ok = asyncio.run(
        hook({"tool_name": "Read", "tool_input": {"file_path": "src/ok.py"}}, "t2", None)
    )
    assert ok == {}
    assert len(violations.read_text().splitlines()) == 1
