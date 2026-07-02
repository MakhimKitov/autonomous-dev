"""The container env contract: parsing, defaults, validation."""

from pathlib import Path

import pytest
from dev_agent import env as agentenv
from dev_agent.env import BurstEnv, EnvError

MINIMAL = {agentenv.TASK_ID: "42", agentenv.BURST_ID: "b1"}


def test_minimal_env_parses_with_defaults():
    env = BurstEnv.from_environ(MINIMAL)
    assert env.task_id == "42"
    assert env.burst_id == "b1"
    assert env.role == "dev"
    assert env.burst_kind == "work"
    assert env.repo_dir == Path(agentenv.CONTAINER_REPO_DIR)
    assert env.session_dir == Path(agentenv.CONTAINER_SESSION_DIR)
    assert env.permission_mode == agentenv.DEFAULT_PERMISSION_MODE
    assert env.resume_session_id is None
    assert env.result_path == Path(agentenv.CONTAINER_SESSION_DIR) / agentenv.BURST_RESULT_FILENAME


def test_missing_required_vars_raise():
    with pytest.raises(EnvError, match="AGENT_TASK_ID"):
        BurstEnv.from_environ({agentenv.BURST_ID: "b1"})
    with pytest.raises(EnvError, match="AGENT_BURST_ID"):
        BurstEnv.from_environ({agentenv.TASK_ID: "42"})


def test_full_env_parses():
    env = BurstEnv.from_environ(
        MINIMAL
        | {
            agentenv.ROLE: "tester",
            agentenv.BURST_KIND: "test",
            agentenv.TASK_PROMPT: "do the thing",
            agentenv.REPO_DIR: "/elsewhere/repo",
            agentenv.CLAUDE_CONFIG_DIR: "/tmp/session",
            agentenv.RESUME_SESSION_ID: "sess-1",
            agentenv.MAX_BUDGET_USD: "2.5",
            agentenv.MAX_TURNS: "40",
            agentenv.MODEL: "claude-sonnet-5",
        }
    )
    assert env.role == "tester"
    assert env.burst_kind == "test"
    assert env.max_budget_usd == 2.5
    assert env.max_turns == 40
    assert env.session_dir == Path("/tmp/session")
    assert env.violations_path == Path("/tmp/session") / agentenv.VIOLATIONS_FILENAME


def test_bad_values_raise():
    with pytest.raises(EnvError, match="AGENT_ROLE"):
        BurstEnv.from_environ(MINIMAL | {agentenv.ROLE: "villain"})
    with pytest.raises(EnvError, match="AGENT_BURST_KIND"):
        BurstEnv.from_environ(MINIMAL | {agentenv.BURST_KIND: "party"})
    with pytest.raises(EnvError, match="AGENT_MAX_BUDGET_USD"):
        BurstEnv.from_environ(MINIMAL | {agentenv.MAX_BUDGET_USD: "lots"})
    with pytest.raises(EnvError, match="AGENT_MAX_TURNS"):
        BurstEnv.from_environ(MINIMAL | {agentenv.MAX_TURNS: "3.5"})
