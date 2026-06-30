"""autonomous-dev — an autonomous AI developer that maintains a target repository.

Engine: claude-agent-sdk (Claude Code as an embeddable library). This package
wraps the SDK's agent loop with the configuration, guardrails, and task plumbing
needed to maintain a separate codebase (e.g. the Discord bot repo) autonomously.
"""

from autonomous_dev.cli import main

__all__ = ["main"]
