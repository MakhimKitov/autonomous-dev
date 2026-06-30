"""Command-line entry point: ``autonomous-dev``."""

from __future__ import annotations

import argparse
from pathlib import Path

from autonomous_dev.agent import RunConfig, run_task_sync

PERMISSION_MODES = ["plan", "acceptEdits", "default", "dontAsk", "bypassPermissions"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonomous-dev",
        description="An autonomous AI developer that maintains a target repository.",
    )
    parser.add_argument("prompt", help="The task / instruction for this run.")
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="Path to the target repository the agent operates in (its cwd).",
    )
    parser.add_argument(
        "--mode",
        default="plan",
        choices=PERMISSION_MODES,
        help="Permission mode (v1 default: 'plan' = explore + propose, no edits).",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument(
        "--model",
        default=None,
        help="Model id or alias (e.g. 'opus', 'sonnet'). Defaults to the CLI's model.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"error: --repo is not a directory: {repo}")
        return 2
    config = RunConfig(
        repo_path=repo,
        permission_mode=args.mode,
        max_turns=args.max_turns,
        model=args.model,
    )
    result = run_task_sync(args.prompt, config)
    print(
        f"\n[done] turns={result.num_turns} cost_usd={result.cost_usd} error={result.is_error}"
    )
    return 1 if result.is_error else 0
