"""Spawn burst containers. One container = one burst, then it dies (§6).

Phase 0 shells out to the ``docker`` CLI — no daemon-API dependency. The env
contract comes from ``dev_agent.env`` (single source of truth; the dependency
points control-plane → dev-agent only).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dev_agent import env as agentenv

DEFAULT_IMAGE = "autonomous-dev/dev-agent:latest"


@dataclass(slots=True)
class BurstSpec:
    """Everything needed to run one burst container."""

    task_id: str
    burst_id: str
    volume_dir: Path
    repo_mount: Path
    prompt: str = ""
    role: str = "dev"
    kind: str = "work"
    command: str = "run"  # or "check" (boot smoke, no network)
    image: str = DEFAULT_IMAGE
    resume_session_id: str | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    model: str | None = None
    traceparent: str | None = None
    github_token: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)


class DockerRunner:
    def build_command(self, spec: BurstSpec) -> list[str]:
        env: dict[str, str] = {
            agentenv.TASK_ID: spec.task_id,
            agentenv.BURST_ID: spec.burst_id,
            agentenv.ROLE: spec.role,
            agentenv.BURST_KIND: spec.kind,
            agentenv.CLAUDE_CONFIG_DIR: agentenv.CONTAINER_SESSION_DIR,
        }
        if spec.prompt:
            env[agentenv.TASK_PROMPT] = spec.prompt
        if spec.resume_session_id:
            env[agentenv.RESUME_SESSION_ID] = spec.resume_session_id
        if spec.max_budget_usd is not None:
            env[agentenv.MAX_BUDGET_USD] = str(spec.max_budget_usd)
        if spec.max_turns is not None:
            env[agentenv.MAX_TURNS] = str(spec.max_turns)
        if spec.model:
            env[agentenv.MODEL] = spec.model
        if spec.traceparent:
            env[agentenv.TRACEPARENT] = spec.traceparent
        if spec.github_token:
            env[agentenv.GITHUB_TOKEN] = spec.github_token
        # Exactly one Anthropic credential enters the box, subscription first —
        # the CLI prefers ANTHROPIC_API_KEY over the OAuth token, so passing
        # both would silently switch billing to the API.
        if spec.command == "run":
            oauth = os.environ.get(agentenv.CLAUDE_CODE_OAUTH_TOKEN)
            api_key = os.environ.get(agentenv.ANTHROPIC_API_KEY)
            if oauth:
                env[agentenv.CLAUDE_CODE_OAUTH_TOKEN] = oauth
            elif api_key:
                env[agentenv.ANTHROPIC_API_KEY] = api_key
        env.update(spec.extra_env)

        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"burst-{spec.burst_id}",
            "-v",
            f"{spec.volume_dir}:{agentenv.CONTAINER_SESSION_DIR}",
            "-v",
            f"{spec.repo_mount}:{agentenv.CONTAINER_REPO_DIR}",
        ]
        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [spec.image, spec.command]
        return cmd

    def run(self, spec: BurstSpec) -> int:
        return subprocess.run(self.build_command(spec), check=False).returncode
