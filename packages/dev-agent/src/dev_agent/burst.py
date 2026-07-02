"""Run one work burst: resume the task's session, do the work, record the outcome.

The container lives exactly one burst (ARCHITECTURE.md §6). Durable state is
the branch (GitHub), the session (the mounted volume, harvested by the control
plane) and the burst result file this module writes before exiting.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    query,
)

from dev_agent.contract import build_system_prompt
from dev_agent.env import BurstEnv
from dev_agent.walls import make_pre_tool_use_hook

TEXT_TAIL_CHARS = 4000


@dataclass(slots=True)
class BurstResult:
    """What the control plane learns from a finished burst."""

    task_id: str
    burst_id: str
    role: str
    burst_kind: str
    session_id: str | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None  # advisory under subscription billing
    input_tokens: int | None = None
    output_tokens: int | None = None
    is_error: bool = False
    duration_s: float | None = None
    finished_at: str = ""
    text_tail: str = ""


def _to_options(env: BurstEnv) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(env.repo_dir),
        system_prompt=build_system_prompt(env.role, env.repo_dir),
        resume=env.resume_session_id,
        max_turns=env.max_turns,
        max_budget_usd=env.max_budget_usd,
        permission_mode=env.permission_mode,  # type: ignore[arg-type]
        model=env.model,
        hooks={
            "PreToolUse": [
                HookMatcher(hooks=[make_pre_tool_use_hook(env.repo_dir, env.violations_path)])
            ]
        },
    )


def setup_git(env: BurstEnv) -> None:
    """Commit identity (the App bot) + gh as the credential helper for pushes.

    Only touches the clone's local config; no token is written to disk — git
    asks gh, gh reads GH_TOKEN from the environment.
    """
    if not (env.github_token and (env.repo_dir / ".git").exists()):
        return
    settings = [
        ("user.name", env.git_user_name or "dev-agent"),
        ("user.email", env.git_user_email or "dev-agent@invalid"),
        ("credential.helper", "!gh auth git-credential"),
    ]
    for key, value in settings:
        subprocess.run(
            ["git", "config", "--local", key, value], cwd=env.repo_dir, check=True
        )


async def run_burst(env: BurstEnv) -> BurstResult:
    if not env.task_prompt:
        raise ValueError("a burst needs a task prompt")
    setup_git(env)
    started = time.monotonic()
    result = BurstResult(
        task_id=env.task_id, burst_id=env.burst_id, role=env.role, burst_kind=env.burst_kind
    )
    chunks: list[str] = []
    async for message in query(prompt=env.task_prompt, options=_to_options(env)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, flush=True)
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            result.session_id = message.session_id
            result.num_turns = getattr(message, "num_turns", None)
            result.total_cost_usd = getattr(message, "total_cost_usd", None)
            result.is_error = bool(getattr(message, "is_error", False))
            usage = getattr(message, "usage", None) or {}
            result.input_tokens = usage.get("input_tokens")
            result.output_tokens = usage.get("output_tokens")
    result.duration_s = round(time.monotonic() - started, 3)
    result.finished_at = datetime.now(UTC).isoformat()
    result.text_tail = "".join(chunks)[-TEXT_TAIL_CHARS:]
    write_result(env, result)
    return result


def write_result(env: BurstEnv, result: BurstResult) -> None:
    env.result_path.parent.mkdir(parents=True, exist_ok=True)
    env.result_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def run_burst_sync(env: BurstEnv) -> BurstResult:
    return asyncio.run(run_burst(env))
