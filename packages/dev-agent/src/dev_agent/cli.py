"""Container entrypoint: ``dev-agent run`` (a burst) / ``dev-agent check`` (boot smoke).

``check`` verifies the box is viable — env contract satisfied, clone present,
``claude`` CLI on PATH — without touching the network. It is the M0 exit
criterion ("docker run boots an agent in a clone") and the first thing to run
when debugging an image.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime

from dev_agent.burst import run_burst_sync
from dev_agent.env import BurstEnv, EnvError


def _check(env: BurstEnv) -> int:
    findings: dict[str, bool | str] = {
        "env": True,
        "claude_cli": bool(shutil.which("claude")),
        "repo_dir": env.repo_dir.is_dir(),
        "git_repo": (env.repo_dir / ".git").exists(),
        "session_dir": env.session_dir.is_dir(),
        "at": datetime.now(UTC).isoformat(),
    }
    ok = all(v is True for k, v in findings.items() if k != "at")
    env.result_path.parent.mkdir(parents=True, exist_ok=True)
    env.result_path.write_text(
        json.dumps({"check": findings, "ok": ok}, indent=2), encoding="utf-8"
    )
    print(json.dumps(findings, indent=2))
    print("check:", "ok" if ok else "FAILED")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dev-agent",
        description="Burst worker — configured entirely by environment (see dev_agent.env).",
    )
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check"])
    args = parser.parse_args(argv)

    try:
        env = BurstEnv.from_environ()
    except EnvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        return _check(env)

    result = run_burst_sync(env)
    print(
        f"\n[burst done] task={result.task_id} burst={result.burst_id} "
        f"session={result.session_id} turns={result.num_turns} "
        f"cost_usd={result.total_cost_usd} error={result.is_error}"
    )
    return 1 if result.is_error else 0


if __name__ == "__main__":
    sys.exit(main())
