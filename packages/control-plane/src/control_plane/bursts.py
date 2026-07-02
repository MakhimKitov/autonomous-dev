"""One burst, end to end — the choreography the CLI and the scheduler share.

prepare volume → mint GitHub token → burst span → docker run (log tee) →
harvest → ledger. This is the proven M0/M1 manual-driver path, extracted so
the M2 scheduler runs exactly the same code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from control_plane.github_auth import (
    AppCredentials,
    bot_git_identity,
    mint_installation_token,
)
from control_plane.models import BurstRecord, BurstStatus, TaskRecord, utc_now
from control_plane.runner import DEFAULT_IMAGE, BurstSpec, DockerRunner, resolve_model
from control_plane.store import LocalSessionArchive, SqliteLedger
from control_plane.telemetry import burst_span
from control_plane.volume import HarvestOutcome, harvest_volume, prepare_volume


@dataclass(slots=True)
class PlatformStores:
    """The platform's data root and its two stores, opened together."""

    data_dir: Path
    archive: LocalSessionArchive
    ledger: SqliteLedger

    @classmethod
    def open(cls, data_dir: Path) -> PlatformStores:
        data_dir = data_dir.expanduser().resolve()
        return cls(
            data_dir=data_dir,
            archive=LocalSessionArchive(data_dir / "sessions"),
            ledger=SqliteLedger(data_dir / "ledger.sqlite3"),
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
    runner: DockerRunner | None = None,
) -> BurstOutcome:
    """Run one burst container against the task and fold the results back in.

    May raise GitHubAuthError (before the container starts) — callers decide
    whether that aborts (CLI) or waits for the next tick (scheduler).
    """
    runner = runner or DockerRunner()
    model = resolve_model(model)
    codex_auth = codex_auth_dir()
    if mode == "run":
        print(f"[model] {model}")
        print(f"[codex] {'auth mounted' if codex_auth else 'no auth — pre-PR review unavailable'}")

    github_token = git_name = git_email = None
    if mode == "run":
        creds = AppCredentials.from_env()
        if creds is not None:
            minted = mint_installation_token(creds)
            github_token = minted.token
            git_name, git_email = bot_git_identity(minted, creds.app_id)
            print(f"[github] burst pushes as {git_name} (token expires {minted.expires_at})")

    burst_id = burst_id or uuid.uuid4().hex[:12]
    volume = prepare_volume(stores.data_dir / "volumes", burst_id, task.task_id, stores.archive)
    resume = None if fresh else task.session_id

    burst = BurstRecord(burst_id=burst_id, task_id=task.task_id, kind=kind, role=role, model=model)
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
        )
        log_path = stores.data_dir / "logs" / f"{burst_id}.log"
        exit_code = runner.run(spec, log_path=log_path)
        burst.exit_code = exit_code
        burst.status = BurstStatus.SUCCEEDED if exit_code == 0 else BurstStatus.FAILED
        burst.finished_at = utc_now()
        span.set_attribute("burst.exit_code", exit_code)

        outcome = harvest_volume(volume, burst, stores.archive, stores.ledger)
        if burst.model:
            span.set_attribute("burst.model", burst.model)  # resolved beats requested
        stores.ledger.finish_burst(burst)
        task.session_id = burst.session_id or task.session_id
        task.transcript_key = outcome.transcript_key
        task.transcript_checksum = outcome.transcript_checksum
        stores.ledger.upsert_task(task)

    print(
        f"[burst {burst_id}] exit={exit_code} status={burst.status} "
        f"model={burst.model} session={burst.session_id} violations={outcome.violation_count} "
        f"transcript={outcome.transcript_key} log={log_path}"
    )
    return BurstOutcome(burst=burst, harvest=outcome, exit_code=exit_code, log_path=log_path)
