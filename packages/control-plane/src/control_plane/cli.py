"""Manual burst driver — the M0/M1 way to run the choreography end to end.

``control-plane burst`` does exactly what the scheduler (M2) will do per event:
prepare volume → open burst span → docker run → harvest → ledger. Driving it by
hand first keeps every seam honest before the loop automates it.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from opentelemetry import trace

from control_plane.models import BurstRecord, BurstStatus, TaskRecord, utc_now
from control_plane.runner import DEFAULT_IMAGE, BurstSpec, DockerRunner
from control_plane.store import LocalSessionArchive, SqliteLedger
from control_plane.telemetry import burst_span, init_telemetry
from control_plane.volume import harvest_volume, prepare_volume


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-plane")
    sub = parser.add_subparsers(dest="command", required=True)

    burst = sub.add_parser("burst", help="run one burst container against a task")
    burst.add_argument("--task-id", required=True, help="the issue id this burst works")
    burst.add_argument("--repo", type=Path, required=True, help="path to the product clone to mount")
    burst.add_argument("--prompt", default="", help="the task text (M0/M1: injected directly)")
    burst.add_argument("--prompt-file", type=Path, help="read the task text from a file")
    burst.add_argument("--mode", default="run", choices=["run", "check"])
    burst.add_argument("--image", default=DEFAULT_IMAGE)
    burst.add_argument("--data-dir", type=Path, default=Path("data"), help="platform store root")
    burst.add_argument("--fresh", action="store_true", help="ignore any stored session (no resume)")
    burst.add_argument("--budget", type=float, default=None, help="max_budget_usd for the burst")
    burst.add_argument("--max-turns", type=int, default=None)
    burst.add_argument("--model", default=None)
    return parser


def _run_burst(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(f"error: --repo is not a directory: {repo}", file=sys.stderr)
        return 2
    prompt = args.prompt
    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    if args.mode == "run" and not prompt:
        print("error: a run burst needs --prompt or --prompt-file", file=sys.stderr)
        return 2

    init_telemetry()
    data_dir = args.data_dir.expanduser().resolve()
    archive = LocalSessionArchive(data_dir / "sessions")
    ledger = SqliteLedger(data_dir / "ledger.sqlite3")

    task = ledger.get_task(args.task_id) or TaskRecord(task_id=args.task_id, repo=str(repo))
    burst_id = uuid.uuid4().hex[:12]
    volume = prepare_volume(data_dir / "volumes", burst_id, args.task_id, archive)
    resume = None if args.fresh else task.session_id

    burst = BurstRecord(burst_id=burst_id, task_id=args.task_id)
    with burst_span(args.task_id, burst_id, burst.role, burst.kind) as (span, traceparent):
        burst.trace_id = f"{span.get_span_context().trace_id:032x}"
        ledger.record_burst(burst)
        spec = BurstSpec(
            task_id=args.task_id,
            burst_id=burst_id,
            volume_dir=volume,
            repo_mount=repo,
            prompt=prompt,
            command=args.mode,
            image=args.image,
            resume_session_id=resume,
            max_budget_usd=args.budget,
            max_turns=args.max_turns,
            model=args.model,
            traceparent=traceparent,
        )
        exit_code = DockerRunner().run(spec)
        burst.exit_code = exit_code
        burst.status = BurstStatus.SUCCEEDED if exit_code == 0 else BurstStatus.FAILED
        burst.finished_at = utc_now()
        span.set_attribute("burst.exit_code", exit_code)

        outcome = harvest_volume(volume, burst, archive, ledger)
        ledger.finish_burst(burst)
        task.session_id = burst.session_id or task.session_id
        task.transcript_key = outcome.transcript_key
        task.transcript_checksum = outcome.transcript_checksum
        ledger.upsert_task(task)

    print(
        f"[burst {burst_id}] exit={exit_code} status={burst.status} "
        f"session={burst.session_id} violations={outcome.violation_count} "
        f"transcript={outcome.transcript_key}"
    )
    trace.get_tracer_provider().shutdown()  # type: ignore[attr-defined]
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "burst":
        return _run_burst(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
