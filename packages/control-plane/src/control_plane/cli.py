"""The control-plane CLI.

``burst`` — the M0/M1 manual driver: one burst, by hand. ``run`` — the M2
scheduler: the same choreography, driven unattended by the reconciler loop
(SCHEDULER.md). ``stop`` — the kill switch. ``github-check`` — App preflight.
``migrate`` — the Postgres ledger's schema, from numbered SQL (STORES.md).
``backfill`` — the one-shot that carries MVP history into the service stores (A6).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import structlog
from dotenv import load_dotenv
from opentelemetry import trace

from control_plane.backfill import BackfillError, run_backfill
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
from control_plane.migrate import MigrationError, migrate, status
from control_plane.models import TaskRecord, task_id_for
from control_plane.review import REVIEWER_APP_PREFIX, ReviewPolicy, ReviewPolicyError
from control_plane.runner import DEFAULT_IMAGE
from control_plane.scheduler import STOP_FILENAME, Rails, Scheduler
from control_plane.store import (
    LEDGER_DSN_ENV,
    LEDGER_FILENAME,
    ArchiveError,
    LocalSessionArchive,
    open_archive,
    open_ledger,
)
from control_plane.telemetry import init_telemetry
from control_plane.testing import E2EPolicy, E2EPolicyError
from dev_agent.log import configure_logging

log = structlog.get_logger()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-plane")
    sub = parser.add_subparsers(dest="command", required=True)

    burst = sub.add_parser("burst", help="run one burst container against a task, by hand")
    # An issue number, not a task id: the id is derived (A6). --github names the
    # product when the clone's origin can't (a fixture, a detached checkout).
    burst.add_argument("--issue", type=int, required=True, help="the issue number this burst works")
    burst.add_argument("--repo", type=Path, required=True, help="path to the product clone to mount")
    burst.add_argument("--github", default=None, help="owner/name (default: parsed from --repo's origin)")
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

    migrate_cmd = sub.add_parser("migrate", help="apply pending schema migrations to the Postgres ledger")
    migrate_cmd.add_argument("--dsn", default=None, help=f"Postgres DSN (default: ${LEDGER_DSN_ENV})")
    migrate_cmd.add_argument(
        "--check",
        action="store_true",
        help="report applied/pending and exit 1 if anything is pending; writes nothing",
    )

    backfill_cmd = sub.add_parser(
        "backfill",
        help="one-shot: carry the MVP's single-node history into the service stores",
    )
    backfill_cmd.add_argument(
        "--repository", required=True, help="owner/name — the product those rows belong to"
    )
    backfill_cmd.add_argument("--data-dir", type=Path, default=Path("data"), help="the source root")
    backfill_cmd.add_argument(
        "--dry-run", action="store_true", help="print the plan and write nothing"
    )

    sub.add_parser(
        "github-check",
        help="preflight the GitHub App: mint a token, show installation + permissions",
    )
    return parser


def _backfill(args: argparse.Namespace) -> int:
    """Move the experiment's history into whatever the toggles now point at.

    The source is always the single-node pair under --data-dir; the destination is
    whatever `LEDGER_DSN` / `ARCHIVE_URL` name, so pointing at nothing is a mistake
    worth catching here rather than discovering as a no-op copy onto itself.
    """
    data_dir = args.data_dir.expanduser().resolve()
    if not os.environ.get(LEDGER_DSN_ENV, "").strip():
        print(f"error: set {LEDGER_DSN_ENV} — the backfill's destination is the service ledger",
              file=sys.stderr)
        return 2

    destination = open_ledger(data_dir)
    source_archive = LocalSessionArchive(data_dir)
    destination_archive = open_archive(data_dir)
    try:
        plan = run_backfill(
            data_dir,
            args.repository,
            destination,
            source_archive,
            destination_archive,
            dry_run=args.dry_run,
        )
    except (BackfillError, ArchiveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        destination.close()

    print(plan.render())
    if args.dry_run:
        print("\n(dry run — nothing was written)")
    return 0


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

    slug = args.github or slug_from_origin(repo)
    if not slug:
        print(
            "error: pass --github owner/name (the clone's origin names no GitHub repo)",
            file=sys.stderr,
        )
        return 2

    init_telemetry()
    stores = PlatformStores.open(args.data_dir)
    # Derived, not typed: the same id the scheduler would compute for this issue,
    # so a hand-run burst joins the same task rather than making a second one.
    task_id = task_id_for(slug, args.issue)
    task = stores.ledger.get_task(task_id) or TaskRecord(
        task_id=task_id, repository=slug, issue_number=args.issue
    )
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
    log.info(
        "scheduler starting",
        repo=slug,
        owner=owner,
        clone=str(platform_clone),
        interval_s=interval,
        rails=str(rails),
    )
    log.info(
        "switchboard",
        review=review.describe(),
        reviewer=reviewer_login,
        e2e=testing.describe(),
        gate=owner,
    )

    def _shutdown(signum, frame):  # noqa: ARG001 — signal handler shape
        scheduler.stop_path.touch()
        if scheduler.current_burst_id:
            subprocess.run(
                ["docker", "kill", f"burst-{scheduler.current_burst_id}"],
                check=False,
                capture_output=True,
            )
        log.info("stop requested — exiting after this tick")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            scheduler.tick()
        except (GitHubApiError, GitHubAuthError) as exc:
            log.warning("github error — retrying next tick", error=str(exc))
        if scheduler.stop_requested():
            log.info("STOP flag present — exiting")
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
    log.info("stop flag written", flag=str(flag))
    # Ask whichever ledger this deployment has. Guarded so `stop` before any run
    # doesn't create an empty SQLite file just to find nothing in it.
    dsn_configured = bool(os.environ.get(LEDGER_DSN_ENV, "").strip())
    if dsn_configured or (data_dir / LEDGER_FILENAME).is_file():
        for burst in open_ledger(data_dir).running_bursts():
            subprocess.run(["docker", "kill", f"burst-{burst.burst_id}"], check=False, capture_output=True)
            log.info("live burst killed", container=f"burst-{burst.burst_id}")
    log.info("the scheduler exits before its next spawn — remove the flag to allow `run` again")
    return 0


def _migrate(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get(LEDGER_DSN_ENV, "").strip()
    if not dsn:
        print(
            f"error: no Postgres DSN — pass --dsn or set {LEDGER_DSN_ENV}. "
            "Unset means the single-node SQLite ledger, which needs no migration.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.check:
            applied, todo = status(dsn)
            for version in applied:
                print(f"applied  {version}")
            for migration in todo:
                print(f"pending  {migration}")
            return 1 if todo else 0
        applied_now = migrate(dsn)
    except (MigrationError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if applied_now:
        log.info("schema migrated", applied=len(applied_now), head=str(applied_now[-1]))
    else:
        log.info("schema up to date — nothing to apply")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # the platform's .env; real env vars win — bursts still get an explicit env
    configure_logging()  # console on a TTY, logfmt everywhere else (dev_agent.log)
    args = build_parser().parse_args(argv)
    if args.command == "burst":
        return _run_burst(args)
    if args.command == "run":
        return _run_scheduler(args)
    if args.command == "stop":
        return _stop(args)
    if args.command == "migrate":
        return _migrate(args)
    if args.command == "backfill":
        return _backfill(args)
    if args.command == "github-check":
        return _github_check()
    return 2


if __name__ == "__main__":
    sys.exit(main())
