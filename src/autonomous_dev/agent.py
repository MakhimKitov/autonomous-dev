"""Core agent loop: run a single maintenance task against a target repository.

v1 is deliberately thin — it proves the claude-agent-sdk pipeline end to end and
gives us a clean seam to grow into. The deterministic guardrail layer
(deny ``private/``, deny prod deploy, ``ops/bin`` allowlist, ``PreToolUse`` hooks,
budget caps, sandboxing) is the next build session; see CLAUDE.md → Roadmap.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

# Conservative default toolset for v1. Edits are gated by ``permission_mode``.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "Bash", "Edit", "Write")


@dataclass(slots=True)
class RunConfig:
    """Parameters for a single task run."""

    repo_path: Path
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    permission_mode: str = "plan"  # v1 default: explore + propose, never auto-edit
    max_turns: int | None = 40
    model: str | None = None

    def to_options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=str(self.repo_path),
            allowed_tools=list(self.allowed_tools),
            permission_mode=self.permission_mode,
            max_turns=self.max_turns,
            model=self.model,
        )


@dataclass(slots=True)
class RunResult:
    """Outcome of a run: collected assistant text plus cost/usage from the SDK."""

    text: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    is_error: bool = False


async def run_task(prompt: str, config: RunConfig) -> RunResult:
    """Run one task against the target repo, streaming assistant text to stdout."""
    chunks: list[str] = []
    result = RunResult()
    async for message in query(prompt=prompt, options=config.to_options()):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, flush=True)
                    chunks.append(block.text)
        elif isinstance(message, ResultMessage):
            result.cost_usd = getattr(message, "total_cost_usd", None)
            result.num_turns = getattr(message, "num_turns", None)
            result.is_error = bool(getattr(message, "is_error", False))
    result.text = "\n".join(chunks)
    return result


def run_task_sync(prompt: str, config: RunConfig) -> RunResult:
    """Synchronous wrapper around :func:`run_task`."""
    return asyncio.run(run_task(prompt, config))
