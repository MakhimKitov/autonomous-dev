"""Golden replay — one archived node, one task, run under each runtime (G4).

The isolation bake-off's method (TO-BE §6): the session tree's nodes are
immutable fixtures, so the same node and the same task can be run as an
identical burst under every candidate `RuntimeClass`, and what differs between
the runs is the runtime. This module runs those trials and emits the criteria
matrix; the decision (G5) is a DR with that matrix attached, and the M4 red-team
suite (G6) replays its attack scenarios through the same function.

A replay is **not a burst of the task**. It writes no ledger row — a trial that
became a node would grow the task's session tree from a fixture — and it holds
no GitHub credential, so a dev node replayed under three runtimes cannot push
three branches. What it does share with a burst is everything below the
scheduler: the spec, the runner's three phases, the archive. A trial's snapshot
is archived under its own key, so "the harvest works on this runtime" is a row
of the matrix, and nothing reads that key back.

What a fixture is (`GoldenBurst.load`) — a JSON file:

``name``, ``task_id``, ``image``
    what is replayed and in which agent image;
``command``
    ``run`` (the engine, on this machine's credential — it costs what the burst
    costs), ``check`` (the image's boot smoke: no network, no key, no spend) or
    ``walls`` (the M4 probes, G6: the box tries its own walls, and the matrix
    carries the breach rate);
``node``
    the archived snapshot to start from: ``{"key", "checksum"}`` as the ledger
    recorded them, or ``{"seed": "<dir>"}`` — a directory next to the fixture,
    archived as the node at replay time, for a fixture that ships its own;
    absent, the burst starts empty;
``resume_session_id``, ``prompt``, ``role``, ``kind``, ``model``,
``max_turns``, ``max_budget_usd``
    the burst's own fields, as `BurstSpec` has them;
``clone``
    ``{"url", "ref"}`` — the working tree, pinned to a commit so every trial
    gets the same one.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.runner import BurstSpec, Runner, RunnerError
from control_plane.store import ArchiveError, SessionArchive, session_key
from control_plane.volume import Snapshot

# How a trial under the substrate's own default runtime is labelled — the
# baseline column of every matrix, and the one `runtime_class=None` runs.
DEFAULT_RUNTIME = "default"

# The rows of the matrix, in order, with what each one measures. The bake-off
# criteria the harness can measure; the others are named in `NOT_MEASURED` so
# the matrix says what it does not say.
CRITERIA: tuple[tuple[str, str], ...] = (
    ("outcome", "what the burst ended as — the same on every runtime, or a finding"),
    ("exit_code", "the agent container's exit code"),
    ("restored", "the node's session was on the volume (snapshot restore)"),
    ("archived", "the harvest archived the trial's volume (snapshot save)"),
    ("breaches", "walls bursts: probes that got out, over probes attempted (M4, target 0)"),
    ("boot_s", "pod created → agent started: scheduling, sandbox boot, init (whole seconds)"),
    ("init_s", "the init phase: the node's GET and the clone"),
    ("agent_s", "the agent container's own run"),
    ("harvest_s", "the harvest phase: the result and snapshot PUTs"),
    ("materialize_s", "host side: the phase that hands the node in"),
    ("run_s", "host side: the burst, start to agent exit and harvest"),
    ("collect_s", "host side: the phase that reads the burst's record back"),
)

NOT_MEASURED = {
    "egress policy enforcement": "the M4 suite's (G6), replayed through this harness",
    "ops burden on our cluster": "a judgment, recorded in the bake-off DR (G5)",
}


COMMANDS = ("run", "check", "walls")


class ReplayError(RuntimeError):
    """The fixture cannot be replayed as written."""


@dataclass(frozen=True, slots=True)
class GoldenBurst:
    """One node and one task — what every trial runs identically."""

    name: str
    task_id: str
    image: str
    command: str = "run"
    prompt: str = ""
    role: str = "dev"
    kind: str = "work"
    node: Snapshot | None = None
    seed: Path | None = None
    resume_session_id: str | None = None
    clone: tuple[str, str] | None = None
    model: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None

    @classmethod
    def load(cls, path: Path) -> GoldenBurst:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayError(f"{path}: not a readable fixture: {exc}") from exc
        missing = [name for name in ("name", "task_id", "image") if not raw.get(name)]
        if missing:
            raise ReplayError(f"{path}: missing {', '.join(missing)}")
        if raw.get("command", "run") not in COMMANDS:
            raise ReplayError(f"{path}: command must be one of {', '.join(COMMANDS)}")
        node = seed = None
        spec = raw.get("node")
        if spec is not None:
            if "seed" in spec:
                seed = (path.parent / spec["seed"]).resolve()
                if not seed.is_dir():
                    raise ReplayError(f"{path}: seed {seed} is not a directory")
            elif spec.get("key") and spec.get("checksum"):
                node = Snapshot(key=spec["key"], checksum=spec["checksum"])
            else:
                raise ReplayError(f"{path}: node needs a seed, or a key and its checksum")
        clone = None
        if raw.get("clone") is not None:
            if not (raw["clone"].get("url") and raw["clone"].get("ref")):
                raise ReplayError(f"{path}: clone needs a url and a ref")
            clone = (raw["clone"]["url"], raw["clone"]["ref"])
        return cls(
            name=raw["name"],
            task_id=raw["task_id"],
            image=raw["image"],
            command=raw.get("command", "run"),
            prompt=raw.get("prompt", ""),
            role=raw.get("role", "dev"),
            kind=raw.get("kind", "work"),
            node=node,
            seed=seed,
            resume_session_id=raw.get("resume_session_id"),
            clone=clone,
            model=raw.get("model"),
            max_turns=raw.get("max_turns"),
            max_budget_usd=raw.get("max_budget_usd"),
        )


@dataclass(slots=True)
class Trial:
    """One run of the golden burst under one runtime."""

    runtime: str
    burst_id: str
    exit_code: int | None = None
    result: dict | None = None
    archived: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    @property
    def outcome(self) -> str:
        if self.error is not None:
            return "did not run"
        if self.result is None:
            return "no result"
        if "check" in self.result:  # the boot smoke's own shape
            return "succeeded" if self.result.get("ok") else "failed (check)"
        if "walls" in self.result:  # the M4 probes' own shape
            if self.result.get("breaches"):
                return "breached"
            return "held" if self.result.get("controls_held") else "failed (a control did not hold)"
        if self.result.get("is_error"):
            return f"failed ({self.result.get('subtype') or 'no subtype'})"
        return "succeeded"

    @property
    def restored(self) -> bool | None:
        """Whether the node's session reached the volume, where the burst can
        say: `check` reports it outright, an engine run by resuming at all.
        None when there was nothing to restore, or no way to tell."""
        if self.result is None:
            return None
        check = self.result.get("check")
        if isinstance(check, dict):
            return check.get("resume_transcript")
        return None

    def row(self, criterion: str):
        if criterion == "outcome":
            return self.outcome
        if criterion == "exit_code":
            return self.exit_code
        if criterion == "restored":
            return self.restored
        if criterion == "archived":
            return self.archived if self.error is None else None
        if criterion == "breaches":
            if self.result is None or "walls" not in self.result:
                return None
            return f"{self.result.get('breaches', 0)}/{self.result.get('attempted', 0)}"
        return self.timings.get(criterion)


def replay(
    golden: GoldenBurst,
    runtimes: list[str],
    runner: Runner,
    archive: SessionArchive,
    work_dir: Path,
    *,
    run_id: str | None = None,
) -> list[Trial]:
    """Run ``golden`` once per runtime, in order, and return the trials.

    ``runtimes`` are `RuntimeClass` names, with `DEFAULT_RUNTIME` for the
    substrate's own. A trial that cannot run is recorded, not raised — a
    runtime that cannot boot the burst is the bake-off's finding, not the
    harness's crash — and the next trial runs regardless.
    """
    run = run_id or uuid.uuid4().hex[:6]
    node = golden.node
    if golden.seed is not None:
        key = session_key(golden.task_id, f"golden-{golden.name}-{run}")
        node = Snapshot(key=key, checksum=archive.put(key, golden.seed))
    trials = []
    for index, runtime in enumerate(runtimes):
        burst_id = f"golden-{run}-{index}"
        trial = Trial(runtime=runtime, burst_id=burst_id)
        trials.append(trial)
        here = work_dir / burst_id
        try:
            spec = _spec(golden, runner, burst_id, runtime, here)
            started = time.monotonic()
            runner.materialize(spec, node)
            materialized = time.monotonic()
            trial.exit_code = runner.run(spec, log_path=here / "burst.log")
            ran = time.monotonic()
            pod = getattr(runner, "timings", None)
            if pod is not None:
                trial.timings.update(pod(burst_id) or {})
            artifacts = runner.collect(spec)
            collected = time.monotonic()
        except (RunnerError, ArchiveError, ReplayError, OSError) as exc:
            trial.error = str(exc)
            continue
        trial.timings.update(
            materialize_s=round(materialized - started, 3),
            run_s=round(ran - materialized, 3),
            collect_s=round(collected - ran, 3),
        )
        trial.result = artifacts.result
        trial.archived = bool(artifacts.session_key)
    return trials


def _spec(golden: GoldenBurst, runner: Runner, burst_id: str, runtime: str, here: Path) -> BurstSpec:
    """The trial's spec: the golden burst's fields and nothing of a task's — no
    GitHub token, no git identity, no lease — under ``runtime``."""
    repo = here / "repo"
    if golden.clone is not None and not getattr(runner, "pod_side", False):
        _checkout(golden.clone, repo)  # a pod clones for itself; docker mounts this
    else:
        repo.mkdir(parents=True, exist_ok=True)
    return BurstSpec(
        task_id=golden.task_id,
        burst_id=burst_id,
        volume_dir=here / "session",
        repo_mount=repo,
        prompt=golden.prompt,
        role=golden.role,
        kind=golden.kind,
        command=golden.command,
        image=golden.image,
        resume_session_id=golden.resume_session_id,
        max_budget_usd=golden.max_budget_usd,
        max_turns=golden.max_turns,
        model=golden.model,
        archive_as=session_key(golden.task_id, burst_id),
        clone=golden.clone,
        runtime_class=None if runtime == DEFAULT_RUNTIME else runtime,
    )


def _checkout(clone: tuple[str, str], dest: Path) -> None:
    url, ref = clone
    dest.parent.mkdir(parents=True, exist_ok=True)
    for args in (["clone", "--quiet", url, str(dest)], ["-C", str(dest), "checkout", "--quiet", "--detach", ref]):
        done = subprocess.run(["git", *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise ReplayError(f"git {args[0]} failed: {done.stderr.strip()[:300]}")


def matrix(golden: GoldenBurst, trials: list[Trial]) -> dict:
    """The criteria matrix: one row per criterion, one column per runtime."""
    outcomes = {trial.outcome for trial in trials}
    probes: dict[str, dict[str, str]] = {}
    for trial in trials:
        for probe in (trial.result or {}).get("walls") or []:
            verdict = "BREACH" if probe.get("breached") else (
                "held" if probe.get("kind") == "attack" else ("ok" if probe.get("succeeded") else "FAILED")
            )
            probes.setdefault(probe.get("name", "?"), {})[trial.runtime] = verdict
    return {
        "golden": golden.name,
        "task_id": golden.task_id,
        "image": golden.image,
        "command": golden.command,
        "runtimes": [trial.runtime for trial in trials],
        "rows": {name: {t.runtime: t.row(name) for t in trials} for name, _ in CRITERIA},
        "identical": len(outcomes) == 1 and "did not run" not in outcomes,
        "errors": {t.runtime: t.error for t in trials if t.error is not None},
        "breaches": sum(int((t.result or {}).get("breaches") or 0) for t in trials),
        "probes": probes,
        "not_measured": dict(NOT_MEASURED),
    }


def render(table: dict) -> str:
    """The matrix as Markdown — what a DR attaches and a CI summary shows."""
    runtimes = table["runtimes"]
    lines = [
        f"# Golden replay: {table['golden']}",
        "",
        f"`{table['image']}` · `{table['command']}` · task `{table['task_id']}` · "
        + ("identical outcome on every runtime" if table["identical"] else "**outcomes differ**"),
        "",
        "| criterion | " + " | ".join(runtimes) + " |",
        "|---|" + "---|" * len(runtimes),
    ]
    for name, _ in CRITERIA:
        cells = [_cell(table["rows"][name].get(runtime)) for runtime in runtimes]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    if table.get("probes"):
        lines += ["", "| probe | " + " | ".join(runtimes) + " |", "|---|" + "---|" * len(runtimes)]
        for name, verdicts in table["probes"].items():
            lines.append(f"| {name} | " + " | ".join(verdicts.get(r, "—") for r in runtimes) + " |")
    lines += ["", "What the rows measure:", ""]
    lines += [f"- `{name}` — {meaning}" for name, meaning in CRITERIA]
    if table["errors"]:
        lines += ["", "Trials that did not run:", ""]
        lines += [f"- {runtime}: {error}" for runtime, error in table["errors"].items()]
    lines += ["", "Not measured here:", ""]
    lines += [f"- {name} — {where}" for name, where in table["not_measured"].items()]
    return "\n".join(lines) + "\n"


def _cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
