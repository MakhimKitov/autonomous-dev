"""The container environment contract.

Everything a burst needs arrives as environment variables: the control plane sets
them, the container consumes them. ``control_plane.runner`` imports these constants,
so the two sides cannot drift. The dependency points one way only — this package
must never import ``control_plane`` (the image cannot contain platform code).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- variable names we own (the contract) ---
ROLE = "AGENT_ROLE"
TASK_ID = "AGENT_TASK_ID"
BURST_ID = "AGENT_BURST_ID"
BURST_KIND = "AGENT_BURST_KIND"
TASK_PROMPT = "AGENT_TASK_PROMPT"
REPO_DIR = "AGENT_REPO_DIR"
RESUME_SESSION_ID = "AGENT_RESUME_SESSION_ID"
MAX_BUDGET_USD = "AGENT_MAX_BUDGET_USD"
MAX_TURNS = "AGENT_MAX_TURNS"
PERMISSION_MODE = "AGENT_PERMISSION_MODE"
MODEL = "AGENT_MODEL"

# --- standard variables we rely on but don't own ---
CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"
TRACEPARENT = "TRACEPARENT"
# Subscription auth (claude setup-token). NOTE: ANTHROPIC_API_KEY outranks the
# OAuth token in the CLI's precedence — the runner must inject exactly one.
CLAUDE_CODE_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
GITHUB_TOKEN = "GITHUB_TOKEN"

ROLES = ("dev", "tester")
BURST_KINDS = ("work", "review", "test")

# --- container filesystem contract (mount points, well-known files) ---
CONTAINER_SESSION_DIR = "/session"  # control-plane-owned volume; CLAUDE_CONFIG_DIR
CONTAINER_REPO_DIR = "/workspace/repo"  # the product clone
BURST_RESULT_FILENAME = "burst-result.json"
VIOLATIONS_FILENAME = "violations.jsonl"

# Walls are hooks + the container + the scoped token — not interactive prompts,
# which nobody is present to answer.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


class EnvError(ValueError):
    """The environment does not satisfy the burst contract."""


@dataclass(slots=True)
class BurstEnv:
    """A parsed, validated burst environment."""

    task_id: str
    burst_id: str
    task_prompt: str = ""
    role: str = "dev"
    burst_kind: str = "work"
    repo_dir: Path = field(default_factory=lambda: Path(CONTAINER_REPO_DIR))
    session_dir: Path = field(default_factory=lambda: Path(CONTAINER_SESSION_DIR))
    resume_session_id: str | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    model: str | None = None

    @property
    def result_path(self) -> Path:
        return self.session_dir / BURST_RESULT_FILENAME

    @property
    def violations_path(self) -> Path:
        return self.session_dir / VIOLATIONS_FILENAME

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> BurstEnv:
        env = dict(os.environ) if environ is None else environ
        missing = [name for name in (TASK_ID, BURST_ID) if not env.get(name)]
        if missing:
            raise EnvError(f"missing required environment: {', '.join(missing)}")

        role = env.get(ROLE, "dev")
        if role not in ROLES:
            raise EnvError(f"{ROLE} must be one of {ROLES}, got {role!r}")
        kind = env.get(BURST_KIND, "work")
        if kind not in BURST_KINDS:
            raise EnvError(f"{BURST_KIND} must be one of {BURST_KINDS}, got {kind!r}")

        def _float(name: str) -> float | None:
            raw = env.get(name)
            try:
                return float(raw) if raw else None
            except ValueError as exc:
                raise EnvError(f"{name} must be a number, got {raw!r}") from exc

        def _int(name: str) -> int | None:
            raw = env.get(name)
            try:
                return int(raw) if raw else None
            except ValueError as exc:
                raise EnvError(f"{name} must be an integer, got {raw!r}") from exc

        return cls(
            task_id=env[TASK_ID],
            burst_id=env[BURST_ID],
            task_prompt=env.get(TASK_PROMPT, ""),
            role=role,
            burst_kind=kind,
            repo_dir=Path(env.get(REPO_DIR, CONTAINER_REPO_DIR)),
            session_dir=Path(env.get(CLAUDE_CONFIG_DIR, CONTAINER_SESSION_DIR)),
            resume_session_id=env.get(RESUME_SESSION_ID) or None,
            max_budget_usd=_float(MAX_BUDGET_USD),
            max_turns=_int(MAX_TURNS),
            permission_mode=env.get(PERMISSION_MODE, DEFAULT_PERMISSION_MODE),
            model=env.get(MODEL) or None,
        )
