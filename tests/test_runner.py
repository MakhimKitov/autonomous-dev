"""Docker command construction — pure, no docker required."""

from pathlib import Path

from control_plane.runner import BurstSpec, DockerRunner
from dev_agent import env as agentenv


def _spec(**overrides) -> BurstSpec:
    defaults = dict(
        task_id="42",
        burst_id="b1",
        volume_dir=Path("/data/volumes/b1"),
        repo_mount=Path("/clones/product"),
        prompt="fix the bug",
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    )
    return BurstSpec(**(defaults | overrides))


def test_command_shape(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    cmd = DockerRunner().build_command(_spec())
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert cmd[-2:] == ["autonomous-dev/dev-agent:latest", "run"]
    assert f"/data/volumes/b1:{agentenv.CONTAINER_SESSION_DIR}" in cmd
    assert f"/clones/product:{agentenv.CONTAINER_REPO_DIR}" in cmd
    joined = " ".join(cmd)
    assert f"{agentenv.TASK_ID}=42" in joined
    assert f"{agentenv.BURST_ID}=b1" in joined
    assert f"{agentenv.TRACEPARENT}=00-" in joined
    assert f"{agentenv.CLAUDE_CONFIG_DIR}={agentenv.CONTAINER_SESSION_DIR}" in joined


def test_subscription_token_preferred_and_exclusive(monkeypatch):
    """Exactly one credential enters the box; the OAuth token wins (the CLI
    would prefer an API key, silently switching billing to the API)."""
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    run_cmd = " ".join(DockerRunner().build_command(_spec()))
    assert f"{agentenv.CLAUDE_CODE_OAUTH_TOKEN}=oat-test" in run_cmd
    assert agentenv.ANTHROPIC_API_KEY not in run_cmd


def test_api_key_is_the_fallback(monkeypatch):
    monkeypatch.delenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, raising=False)
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    run_cmd = " ".join(DockerRunner().build_command(_spec()))
    assert f"{agentenv.ANTHROPIC_API_KEY}=sk-test" in run_cmd


def test_no_credentials_in_check_bursts(monkeypatch):
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    monkeypatch.setenv(agentenv.ANTHROPIC_API_KEY, "sk-test")
    check_cmd = " ".join(DockerRunner().build_command(_spec(command="check")))
    assert agentenv.CLAUDE_CODE_OAUTH_TOKEN not in check_cmd
    assert agentenv.ANTHROPIC_API_KEY not in check_cmd


def test_optional_env_only_when_set(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    minimal = " ".join(DockerRunner().build_command(_spec(prompt="", traceparent=None)))
    assert agentenv.TASK_PROMPT not in minimal
    assert agentenv.TRACEPARENT not in minimal
    assert agentenv.RESUME_SESSION_ID not in minimal

    resumed = " ".join(DockerRunner().build_command(_spec(resume_session_id="sess-9")))
    assert f"{agentenv.RESUME_SESSION_ID}=sess-9" in resumed
