"""Docker command construction — pure, no docker required."""

from pathlib import Path

from control_plane import telemetry
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


def test_optional_env_only_when_set(monkeypatch):
    monkeypatch.delenv(agentenv.ANTHROPIC_API_KEY, raising=False)
    minimal = " ".join(DockerRunner().build_command(_spec(prompt="", traceparent=None)))
    assert agentenv.TASK_PROMPT not in minimal
    assert agentenv.TRACEPARENT not in minimal
    assert agentenv.RESUME_SESSION_ID not in minimal

    resumed = " ".join(DockerRunner().build_command(_spec(resume_session_id="sess-9")))
    assert f"{agentenv.RESUME_SESSION_ID}=sess-9" in resumed


def _env_of(cmd: list[str]) -> dict[str, str]:
    """The `-e KEY=VALUE` pairs the container will actually be given."""
    pairs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]
    return dict(pair.split("=", 1) for pair in pairs)


def test_no_collector_means_no_telemetry_variables(monkeypatch):
    """A presence toggle, like the two stores: unconfigured is not a broken export,
    it is a box that was never told about telemetry (B2)."""
    monkeypatch.delenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, raising=False)
    env = _env_of(DockerRunner().build_command(_spec()))
    assert not [key for key in env if key.startswith(("OTEL_", "CLAUDE_CODE_ENABLE", "CLAUDE_CODE_ENH"))]


def test_the_agents_own_otel_is_configured_from_one_variable(monkeypatch):
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = _env_of(DockerRunner().build_command(_spec(task_id="owner/product#42", role="tester", kind="test")))

    # both switches, because traces are beta-gated behind the second one
    assert env[agentenv.CLAUDE_CODE_ENABLE_TELEMETRY] == "1"
    assert env[agentenv.CLAUDE_CODE_ENHANCED_TELEMETRY_BETA] == "1"
    assert env[agentenv.OTEL_TRACES_EXPORTER] == "otlp"
    assert env[agentenv.OTEL_EXPORTER_OTLP_ENDPOINT] == "http://collector:4318"
    assert env[agentenv.OTEL_EXPORTER_OTLP_PROTOCOL] == "http/protobuf"
    assert env[agentenv.OTEL_SERVICE_NAME] == agentenv.AGENT_SERVICE_NAME

    # the ids the burst span carries, stamped onto every signal the box emits
    assert env[agentenv.OTEL_RESOURCE_ATTRIBUTES] == (
        "task.id=owner/product#42,burst.id=b1,burst.role=tester,burst.kind=test"
    )
    # and the traceparent is still there — it is what makes them one trace
    assert env[agentenv.TRACEPARENT].startswith("00-")


def test_the_box_is_never_given_a_console_exporter(monkeypatch):
    """The SDK reads the CLI's stdout as its message channel, so a console exporter
    doesn't export the burst, it corrupts it."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = _env_of(DockerRunner().build_command(_spec()))
    assert "console" not in " ".join(env.values())


def test_content_bodies_stay_out_of_telemetry(monkeypatch):
    """Arguments yes, bodies no. Tool inputs are the experiment's dataset (§9); the
    full input/output bodies and raw API bodies are the transcript's job, and the
    transcript is in the archive with the ledger's checksum over it."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = _env_of(DockerRunner().build_command(_spec()))
    assert env[agentenv.OTEL_LOG_TOOL_DETAILS] == "1"
    assert "OTEL_LOG_TOOL_CONTENT" not in env
    assert "OTEL_LOG_RAW_API_BODIES" not in env


def test_no_metrics_exporter_because_the_ledger_is_the_metrics_store(monkeypatch):
    """TO-BE §12: no emitted-metrics pipeline — the CLI's counters are cost and
    tokens, which the burst row already records as the authoritative copy."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = _env_of(DockerRunner().build_command(_spec()))
    assert agentenv.OTEL_METRICS_EXPORTER not in env


def test_a_check_burst_gets_no_telemetry(monkeypatch):
    """`check` is the no-network boot smoke: no credential, and nothing to export."""
    monkeypatch.setenv(telemetry.AGENT_OTLP_ENDPOINT_ENV, "http://collector:4318")
    env = _env_of(DockerRunner().build_command(_spec(command="check")))
    assert not [key for key in env if key.startswith("OTEL_")]


def test_reviewer_repo_mount_is_read_only():
    cmd = DockerRunner().build_command(_spec(role="reviewer", repo_read_only=True))
    assert any(v.endswith("/workspace/repo:ro") for v in cmd)
    # and the dev burst's stays writable
    cmd = DockerRunner().build_command(_spec())
    assert any(v.endswith("/workspace/repo") for v in cmd)
    assert not any(v.endswith(":ro") for v in cmd)
