"""Docker command construction — pure, no docker required."""

from pathlib import Path

from control_plane.runner import DEFAULT_MODEL, MODEL_ENV, BurstSpec, DockerRunner, resolve_model
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


def test_github_token_feeds_both_git_and_gh(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    cmd = " ".join(
        DockerRunner().build_command(
            _spec(github_token="ghs_x", git_user_name="bot[bot]", git_user_email="b@nr.gh")
        )
    )
    assert f"{agentenv.GITHUB_TOKEN}=ghs_x" in cmd
    assert f"{agentenv.GH_TOKEN}=ghs_x" in cmd
    assert f"{agentenv.GIT_USER_NAME}=bot[bot]" in cmd
    assert f"{agentenv.GIT_USER_EMAIL}=b@nr.gh" in cmd


def test_run_tees_output_to_log_file(tmp_path, monkeypatch, capsys):
    runner = DockerRunner()
    monkeypatch.setattr(
        runner, "build_command", lambda spec: ["sh", "-c", "echo agent-line; echo oops 1>&2"]
    )
    log_path = tmp_path / "logs" / "b1.log"
    code = runner.run(_spec(), log_path=log_path)
    assert code == 0
    logged = log_path.read_text(encoding="utf-8")
    assert "agent-line" in logged
    assert "oops" in logged  # stderr captured too
    assert "agent-line" in capsys.readouterr().out  # still streams live


def test_run_propagates_exit_code(tmp_path, monkeypatch):
    runner = DockerRunner()
    monkeypatch.setattr(runner, "build_command", lambda spec: ["sh", "-c", "echo dying; exit 3"])
    log_path = tmp_path / "b2.log"
    assert runner.run(_spec(), log_path=log_path) == 3
    assert "dying" in log_path.read_text(encoding="utf-8")


def test_codex_auth_mounted_only_for_run_bursts(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    spec_run = _spec(codex_auth_dir=Path("/host/.codex"))
    spec_check = _spec(codex_auth_dir=Path("/host/.codex"), command="check")
    assert "/host/.codex:/home/agent/.codex" in DockerRunner().build_command(spec_run)
    assert "/host/.codex:/home/agent/.codex" not in DockerRunner().build_command(spec_check)


def test_seccomp_profile_applied_and_valid(monkeypatch):
    """Issue #3: bursts run under the userns-enabled profile, never unconfined."""
    import json

    from control_plane.runner import SECCOMP_PROFILE

    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    cmd = DockerRunner().build_command(_spec())
    assert f"seccomp={SECCOMP_PROFILE}" in cmd
    assert "unconfined" not in " ".join(cmd)

    profile = json.loads(SECCOMP_PROFILE.read_text(encoding="utf-8"))
    assert profile["defaultAction"] != "SCMP_ACT_ALLOW"  # still an allowlist
    userns_rules = [
        rule
        for rule in profile["syscalls"]
        if "unshare" in rule.get("names", []) and rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
    ]
    assert userns_rules, "userns syscalls must be allowed unconditionally"


def test_model_env_var_and_spec_passthrough(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    cmd = " ".join(DockerRunner().build_command(_spec(model="claude-opus-4-8")))
    assert f"{agentenv.MODEL}=claude-opus-4-8" in cmd


def test_model_policy_precedence(monkeypatch):
    """--model beats DEV_AGENT_MODEL beats the pinned default; never the CLI's whim."""
    monkeypatch.delenv(MODEL_ENV, raising=False)
    assert resolve_model(None) == DEFAULT_MODEL
    monkeypatch.setenv(MODEL_ENV, "claude-haiku-4-5-20251001")
    assert resolve_model(None) == "claude-haiku-4-5-20251001"
    assert resolve_model("claude-opus-4-8") == "claude-opus-4-8"


def test_optional_env_only_when_set(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    minimal = " ".join(DockerRunner().build_command(_spec(prompt="", traceparent=None)))
    assert agentenv.TASK_PROMPT not in minimal
    assert agentenv.TRACEPARENT not in minimal
    assert agentenv.RESUME_SESSION_ID not in minimal

    resumed = " ".join(DockerRunner().build_command(_spec(resume_session_id="sess-9")))
    assert f"{agentenv.RESUME_SESSION_ID}=sess-9" in resumed
