"""The control-plane CLI.

``burst`` — the M0/M1 manual driver: one burst, by hand. ``run`` — the M2
scheduler: the same choreography, driven unattended by the reconciler loop
(SCHEDULER.md). ``stop`` — the kill switch. ``github-check`` — App preflight.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from opentelemetry import trace

from control_plane.bursts import PlatformStores, execute_burst
from control_plane.clone import CloneError, prepare_clone
from control_plane.github_api import COPILOT_TOKEN_ENV, GitHubApi, GitHubApiError, slug_from_origin
from control_plane.github_auth import (
    AppCredentials,
    GitHubAuthError,
    InstallationTokenCache,
    bot_git_identity,
    mint_installation_token,
)
from control_plane.models import TaskRecord
from control_plane.review import REVIEWER_APP_PREFIX, ReviewPolicy, ReviewPolicyError
from control_plane.runner import DEFAULT_IMAGE
from control_plane.scheduler import STOP_FILENAME, Rails, Scheduler
from control_plane.store import SqliteLedger
from control_plane.telemetry import init_telemetry
from control_plane.testing import E2EPolicy, E2EPolicyError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-plane")
    sub = parser.add_subparsers(dest="command", required=True)

    burst = sub.add_parser("burst", help="run one burst container against a task, by hand")
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
    burst.add_argument(
        "--model",
        default=None,
        help="model for this burst (default: $DEV_AGENT_MODEL, else the pinned platform default)",
    )

    run = sub.add_parser("run", help="the M2 scheduler: reconcile GitHub ↔ ledger, spawn bursts")
    run.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="operator clone — used only to derive owner/name; bursts mount the platform clone",
    )
    run.add_argument("--github", default=None, help="owner/name (default: parsed from --repo's origin)")
    run.add_argument("--owner", default=None, help="the human owner's login (default: the repo owner)")
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--image", default=DEFAULT_IMAGE)
    run.add_argument("--model", default=None)
    run.add_argument("--interval", type=int, default=None, help="seconds between ticks (default: rails)")
    run.add_argument("--once", action="store_true", help="one tick, then exit")

    stop = sub.add_parser("stop", help="kill switch: write the STOP flag and kill the live burst")
    stop.add_argument("--data-dir", type=Path, default=Path("data"))

    sub.add_parser(
        "github-check",
        help="preflight the GitHub App: mint a token, show installation + permissions",
    )
    return parser


def _github_check() -> int:
    try:
        creds = AppCredentials.from_env()
        if creds is None:
            print("GitHub App not configured (GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH unset)")
            return 2
        token = mint_installation_token(creds)
    except GitHubAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    name, email = bot_git_identity(token, creds.app_id)
    print(f"app:          {token.app_slug}")
    print(f"bot identity: {name} <{email}>")
    print(f"token:        {token.token[:11]}… expires {token.expires_at}")
    print(f"permissions:  {token.permissions}")
    forbidden = {"administration", "merge_queues"} & set(token.permissions)
    if forbidden:
        print(f"⚠ unexpected permissions present: {forbidden}")
    return 0


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
    stores = PlatformStores.open(args.data_dir)
    task = stores.ledger.get_task(args.task_id) or TaskRecord(task_id=args.task_id, repo=str(repo))
    try:
        outcome = execute_burst(
            stores,
            task,
            repo,
            prompt,
            mode=args.mode,
            image=args.image,
            model=args.model,
            max_budget_usd=args.budget,
            max_turns=args.max_turns,
            fresh=args.fresh,
            with_github=True,  # the manual burst mirrors a dev burst
        )
    except GitHubAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    trace.get_tracer_provider().shutdown()  # type: ignore[attr-defined]
    return outcome.exit_code


def _run_scheduler(args: argparse.Namespace) -> int:
    slug = args.github
    if not slug and args.repo is not None:
        slug = slug_from_origin(args.repo.expanduser().resolve())
    if not slug:
        print("error: pass --github owner/name (or --repo, to derive it from origin)", file=sys.stderr)
        return 2
    try:
        creds = AppCredentials.from_env()
    except GitHubAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if creds is None:
        print("error: the scheduler needs the GitHub App configured (it polls as the App)", file=sys.stderr)
        return 2
    try:
        review = ReviewPolicy.from_env()  # a declaration that can't be honored refuses to start
        testing = E2EPolicy.from_env()
    except (ReviewPolicyError, E2EPolicyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if testing.enabled and not review.claude:
        # the funnel (TESTER.md): tests run only after a clean assess — without
        # the claude review provider that trigger never fires
        print("error: E2E=on requires the claude review provider (tests run after a clean assess)", file=sys.stderr)
        return 2

    init_telemetry()
    stores = PlatformStores.open(args.data_dir)
    if (stores.data_dir / STOP_FILENAME).exists():
        print(f"error: STOP flag present ({stores.data_dir / STOP_FILENAME}) — remove it to start", file=sys.stderr)
        return 2

    token_cache = InstallationTokenCache(creds)
    copilot_token = os.environ.get(COPILOT_TOKEN_ENV, "").strip() or None
    api = GitHubApi(slug, token_cache, copilot_token=copilot_token)

    reviewer_api = reviewer_login = None
    if review.claude:  # the claude provider posts as a second App (REVIEWERS.md)
        try:
            reviewer_creds = AppCredentials.from_env(prefix=REVIEWER_APP_PREFIX)
            minted = mint_installation_token(reviewer_creds)
        except GitHubAuthError as exc:
            print(f"error: reviewer App: {exc}", file=sys.stderr)
            return 2
        reviewer_login = minted.bot_login
        reviewer_api = GitHubApi(slug, InstallationTokenCache(reviewer_creds))

    clones_root = stores.data_dir / "clones"

    def sync_clone() -> Path:
        return prepare_clone(clones_root, slug, token_cache())

    def sync_clone_ref(ref: str) -> Path:
        return prepare_clone(clones_root, slug, token_cache(), ref=ref)

    try:
        owner = args.owner or api.repo_owner()
        platform_clone = sync_clone()  # bursts mount this, never the operator's copy (#4)
    except (GitHubApiError, CloneError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    rails = Rails.from_env()
    interval = args.interval if args.interval is not None else rails.poll_interval_s
    scheduler = Scheduler(
        stores,
        api,
        platform_clone,
        owner,
        rails=rails,
        review=review,
        testing=testing,
        reviewer_api=reviewer_api,
        reviewer_login=reviewer_login,
        image=args.image,
        model=args.model,
        sync_clone=sync_clone,
        sync_clone_ref=sync_clone_ref,
    )
    print(f"[scheduler] repo={slug} owner={owner} clone={platform_clone} interval={interval}s rails={rails}")
    reviewer_note = f" · reviewer={reviewer_login}" if reviewer_login else ""
    print(f"[scheduler] review: {review.describe()}{reviewer_note} · e2e={testing.describe()} · gate={owner}")

    def _shutdown(signum, frame):  # noqa: ARG001 — signal handler shape
        scheduler.stop_path.touch()
        if scheduler.current_burst_id:
            subprocess.run(
                ["docker", "kill", f"burst-{scheduler.current_burst_id}"],
                check=False,
                capture_output=True,
            )
        print("[scheduler] stop requested — exiting after this tick")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            scheduler.tick()
        except (GitHubApiError, GitHubAuthError) as exc:
            print(f"[scheduler] GitHub error — retrying next tick: {exc}", file=sys.stderr)
        if scheduler.stop_requested():
            print("[scheduler] STOP flag present — exiting")
            break
        if args.once:
            break
        time.sleep(interval)
    trace.get_tracer_provider().shutdown()  # type: ignore[attr-defined]
    return 0


def _stop(args: argparse.Namespace) -> int:
    data_dir = args.data_dir.expanduser().resolve()
    flag = data_dir / STOP_FILENAME
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    print(f"[stop] flag written: {flag}")
    ledger_path = data_dir / "ledger.sqlite3"
    if ledger_path.is_file():
        for burst in SqliteLedger(ledger_path).running_bursts():
            subprocess.run(["docker", "kill", f"burst-{burst.burst_id}"], check=False, capture_output=True)
            print(f"[stop] docker kill burst-{burst.burst_id}")
    print("[stop] the scheduler exits before its next spawn; remove the flag to allow `run` again")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # the platform's .env; real env vars win — bursts still get an explicit env
    args = build_parser().parse_args(argv)
    if args.command == "burst":
        return _run_burst(args)
    if args.command == "run":
        return _run_scheduler(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "github-check":
        return _github_check()
    return 2


if __name__ == "__main__":
    sys.exit(main())
