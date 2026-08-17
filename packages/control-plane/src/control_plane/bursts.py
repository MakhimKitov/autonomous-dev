"""One burst, end to end — the choreography the CLI and the scheduler share.

prepare volume → mint GitHub token → burst span → docker run (log tee) →
harvest → ledger. This is the proven M0/M1 manual-driver path, extracted so
the M2 scheduler runs exactly the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from control_plane.github_auth import (
    AppCredentials,
    bot_git_identity,
    mint_installation_token,
)
from control_plane.models import BurstRecord, BurstStatus, TaskRecord, utc_now
from control_plane.runner import DEFAULT_IMAGE, BurstSpec, DockerRunner, resolve_model
from control_plane.store import Ledger, SessionArchive, open_archive, open_ledger
from control_plane.telemetry import burst_span
from control_plane.volume import HarvestOutcome, harvest_volume, prepare_volume

log = structlog.get_logger()


@dataclass(slots=True)
class PlatformStores:
    """The platform's data root and its two stores, opened together.

    Both are chosen by presence toggle (`LEDGER_DSN`, `ARCHIVE_URL`) and both are
    held behind their protocol, so nothing downstream of here can tell — or name —
    which backend it got.
    """

    data_dir: Path
    archive: SessionArchive
    ledger: Ledger

    @classmethod
    def open(cls, data_dir: Path) -> PlatformStores:
        data_dir = data_dir.expanduser().resolve()
        return cls(
            data_dir=data_dir,
            archive=open_archive(data_dir),
            ledger=open_ledger(data_dir),
        )


@dataclass(slots=True)
class BurstOutcome:
    burst: BurstRecord
    harvest: HarvestOutcome
    exit_code: int
    log_path: Path

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not (self.harvest.result or {}).get("is_error")


def codex_auth_dir() -> Path | None:
    codex = Path.home() / ".codex"
    return codex if (codex / "auth.json").is_file() else None


def execute_burst(
    stores: PlatformStores,
    task: TaskRecord,
    repo: Path,
    prompt: str,
    *,
    kind: str = "work",
    role: str = "dev",
    mode: str = "run",
    image: str = DEFAULT_IMAGE,
    model: str | None = None,
    max_budget_usd: float | None = None,
    max_turns: int | None = None,
    fresh: bool = False,
    burst_id: str | None = None,
    with_github: bool = False,
    mount_codex: bool = False,
    extra_env: dict[str, str] | None = None,
    runner: DockerRunner | None = None,
) -> BurstOutcome:
    """Run one burst container against the task and fold the results back in.

    ``with_github`` mints the dev App token into the box (dev bursts push;
    reviewer bursts get no GitHub credential at all — REVIEWERS.md).
    ``mount_codex`` mounts the operator's codex auth (slice 3, reviewer only).
    ``extra_env`` injects sandbox credentials — the resolved TEST_ intersection,
    tester bursts only (TESTER.md).

    May raise GitHubAuthError (before the container starts) — callers decide
    whether that aborts (CLI) or waits for the next tick (scheduler).
    """
    runner = runner or DockerRunner()
    model = resolve_model(model)
    codex_auth = codex_auth_dir() if mount_codex else None
    if mode == "run":
        log.info("burst starting", task=task.task_id, kind=kind, role=role, model=model)

    github_token = git_name = git_email = None
    if mode == "run" and with_github:
        creds = AppCredentials.from_env()
        if creds is not None:
            minted = mint_installation_token(creds)
            github_token = minted.token
            git_name, git_email = bot_git_identity(minted, creds.app_id)
            log.info("github token minted", pushes_as=git_name, expires=minted.expires_at)

    burst_id = burst_id or uuid.uuid4().hex[:12]
    # The node this burst descends from: the leaf of the task's session tree, or
    # nothing. fresh = amnesiac (the reviewer), which is exactly "no parent" — so
    # the volume cannot be seeded with the dev's transcripts and the resume is off,
    # from one decision rather than two flags.
    parent = None if fresh else stores.ledger.latest_archived_burst(task.task_id)
    volume = prepare_volume(stores.data_dir / "volumes", burst_id, stores.archive, parent=parent)
    resume = None if fresh else task.session_id

    burst = BurstRecord(
        burst_id=burst_id,
        task_id=task.task_id,
        kind=kind,
        role=role,
        model=model,
        # recorded at spawn, before the container starts: the edge is a fact about
        # what this burst was given, not about what it managed to produce
        resumed_from_burst_id=parent.burst_id if parent is not None else None,
    )
    # The task row goes in before the burst row that points at it. A4's
    # bursts.task_id foreign key is what makes the order load-bearing, but the
    # order was always the honest one: the CLI can be handed a task the ledger has
    # never seen, and a burst is evidence about a task, so the task exists first.
    # The second upsert after the harvest is what records the burst's results.
    stores.ledger.upsert_task(task)
    with burst_span(task.task_id, burst_id, role, kind, model=model) as (span, traceparent):
        burst.trace_id = f"{span.get_span_context().trace_id:032x}"
        stores.ledger.record_burst(burst)
        spec = BurstSpec(
            task_id=task.task_id,
            burst_id=burst_id,
            volume_dir=volume,
            repo_mount=repo,
            prompt=prompt,
            role=role,
            kind=kind,
            command=mode,
            image=image,
            resume_session_id=resume,
            max_budget_usd=max_budget_usd,
            max_turns=max_turns,
            model=model,
            traceparent=traceparent,
            github_token=github_token,
            git_user_name=git_name,
            git_user_email=git_email,
            codex_auth_dir=codex_auth,
            repo_read_only=(role in ("reviewer", "tester")),  # a wall, not an instruction
            extra_env=dict(extra_env or {}),
        )
        log_path = stores.data_dir / "logs" / f"{burst_id}.log"
        exit_code = runner.run(spec, log_path=log_path)
        burst.exit_code = exit_code
        burst.status = BurstStatus.SUCCEEDED if exit_code == 0 else BurstStatus.FAILED
        burst.finished_at = utc_now()
        span.set_attribute("burst.exit_code", exit_code)

        outcome = harvest_volume(
            volume, burst, stores.archive, stores.ledger, archive_session=(role == "dev")
        )
        if burst.model:
            span.set_attribute("burst.model", burst.model)  # resolved beats requested
        stores.ledger.finish_burst(burst)
        if role == "dev":
            # the task's session is the *dev's* memory — a reviewer burst's
            # session must never become what the next dev round resumes.
            # The snapshot itself is on the burst row now (A5): tasks.transcript_*
            # stop being written here and A6 (#17) backfills and drops them.
            task.session_id = burst.session_id or task.session_id
        stores.ledger.upsert_task(task)

    log.info(
        "burst finished",
        burst=burst_id,
        exit=exit_code,
        status=str(burst.status),
        model=burst.model,
        session=burst.session_id,
        violations=outcome.violation_count,
        resumed_from=burst.resumed_from_burst_id,
        snapshot=outcome.session_key,
        log=str(log_path),
    )
    return BurstOutcome(burst=burst, harvest=outcome, exit_code=exit_code, log_path=log_path)
