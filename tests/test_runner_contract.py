"""The `Runner` contract, run against every runner there is.

One suite, parameterized over the runners — `DockerRunner` today, `JobsRunner`
when the cluster arrives (G3, #44) — for the reason the ledger and the archive
each have one (`test_ledger_contract.py`, `test_archive_contract.py`): where a
burst runs must not change what a burst *is* to the control plane. A test that
passes on one runner and fails on another is a bug by definition.

Every test speaks to a **rig**, never to a runner's internals. A rig is what a
substrate has to provide to be tested here, and what G3 adds one of:

- ``rig.runner`` — the `Runner` under test;
- ``rig.burst(script, burst_id)`` — a `BurstSpec` whose burst runs ``script``
  under ``sh`` on that substrate (the scripts below are portable shell: echo,
  exit, sleep, a loop);
- ``rig.packaged_env(spec)`` — the environment the runner would hand the box
  for ``spec``, read back from whatever the runner packages it into;
- ``rig.archive`` — the archive the runner's bytes phases (G1) read and write,
  so the suite can seed a parent snapshot and read back what was collected.

The docker rig is **in-process**: a `DockerRunner` with a shell in the daemon's
seat. Its three docker lines are stood in for — ``docker run`` becomes
``sh -c``, ``docker kill`` a SIGKILL to that shell, ``docker ps`` a probe of
its pid — and everything the class does around them (the pump, the tee, the
clock, the fence) is the real thing. No docker needed, so the suite runs
everywhere the unit tests do.
"""

import contextlib
import os
import signal
from pathlib import Path

import pytest
from control_plane import runner as runner_mod
from control_plane.runner import BurstSpec, DockerRunner, burst_env
from control_plane.store import ArchiveError, LocalSessionArchive
from control_plane.volume import Snapshot
from dev_agent import env as agentenv
from dev_agent.env import BURST_RESULT_FILENAME, VIOLATIONS_FILENAME

RUNNERS = ["docker"]  # G3 (#44) appends "jobs", skipped without a cluster


def _spec(**overrides) -> BurstSpec:
    defaults = dict(
        task_id="owner/product#42",
        burst_id="b1",
        volume_dir=Path("/data/volumes/b1"),
        repo_mount=Path("/clones/product"),
    )
    return BurstSpec(**(defaults | overrides))


class ShellDocker(DockerRunner):
    """`DockerRunner` with the daemon replaced by a shell — see the module doc."""

    def __init__(self, piddir: Path, archive=None):
        super().__init__(archive)
        self.piddir = piddir
        self.scripts: dict[str, str] = {}

    def build_command(self, spec: BurstSpec) -> list[str]:
        # the shell writes its own pid first, so `kill` and `alive` can find it
        # the way docker finds a container by name; and it is told where its
        # volume is under the name the box knows it by, since a burst writes its
        # result into `CLAUDE_CONFIG_DIR` wherever it runs
        pidfile = self.piddir / f"burst-{spec.burst_id}"
        return [
            "sh", "-c",
            f'echo $$ > "$0"; CLAUDE_CONFIG_DIR="$1"; export CLAUDE_CONFIG_DIR; {self.scripts[spec.burst_id]}',
            str(pidfile), str(spec.volume_dir),
        ]

    def _pid(self, burst_id: str) -> int | None:
        pidfile = self.piddir / f"burst-{burst_id}"
        if not pidfile.is_file():
            return None
        text = pidfile.read_text(encoding="utf-8").strip()
        return int(text) if text else None

    def kill(self, burst_id: str) -> None:
        pid = self._pid(burst_id)
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def alive(self, burst_id: str) -> bool:
        pid = self._pid(burst_id)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


class DockerRig:
    def __init__(self, tmp_path: Path):
        self.archive = LocalSessionArchive(tmp_path / "archive")
        self.runner = ShellDocker(tmp_path / "pids", archive=self.archive)
        self.runner.piddir.mkdir()
        self.volumes = tmp_path / "volumes"

    def burst(self, script: str, burst_id: str = "b1") -> BurstSpec:
        self.runner.scripts[burst_id] = script
        return _spec(burst_id=burst_id, volume_dir=self.volumes / burst_id)

    def packaged_env(self, spec: BurstSpec) -> dict[str, str]:
        # the real command, not the shell's: the `-e KEY=VALUE` pairs docker would be given
        cmd = DockerRunner().build_command(spec)
        pairs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]
        return dict(pair.split("=", 1) for pair in pairs)


def _volume_files(rig, spec):
    """What is in the burst's volume, as the rig can see it."""
    return sorted(path for path in spec.volume_dir.rglob("*") if path.is_file())


def _read(rig, spec, relative: str) -> str:
    return (spec.volume_dir / relative).read_text(encoding="utf-8")


def _write(rig, spec, relative: str, text: str) -> None:
    path = spec.volume_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(params=RUNNERS)
def rig(request, tmp_path, monkeypatch):
    """A rig per runner. The heartbeat clock is sped up for the suite — the
    contract is *at least this often*, and the value is the runner module's."""
    monkeypatch.setattr(runner_mod, "HEARTBEAT_POLL_S", 0.05)
    if request.param == "docker":
        return DockerRig(tmp_path)
    raise AssertionError(f"no rig for {request.param!r}")


# --- materialize and collect: the bytes in, the bytes out (G1) ---


def test_a_burst_with_no_parent_starts_empty(rig):
    spec = rig.burst("true")
    rig.runner.materialize(spec, None)
    assert list(_volume_files(rig, spec)) == []


def test_a_parent_snapshot_is_materialized_before_the_burst_runs(rig, tmp_path):
    """What `resume` needs, wherever the burst runs: the parent's transcript
    already in the volume when the box boots. The checksum comes with the key
    and the archive is asked to prove the bytes match it."""
    seed = tmp_path / "seed"
    (seed / "projects").mkdir(parents=True)
    (seed / "projects" / "sess.jsonl").write_text("the parent's turn\n", encoding="utf-8")
    checksum = rig.archive.put("sessions/owner/product#42/parent/session.tar.gz", seed)

    spec = rig.burst("true")
    rig.runner.materialize(
        spec, Snapshot("sessions/owner/product#42/parent/session.tar.gz", checksum)
    )
    assert _read(rig, spec, "projects/sess.jsonl") == "the parent's turn\n"

    with pytest.raises(ArchiveError):
        rig.runner.materialize(
            spec, Snapshot("sessions/owner/product#42/parent/session.tar.gz", "0" * 64)
        )


def test_collect_reads_back_what_the_burst_left(rig):
    """The result file and the violations feed, as bytes — no ledger anywhere
    near a burst's substrate. `fold_artifacts` is what turns them into rows, and
    it runs in the control plane."""
    spec = rig.burst(
        f'printf \'%s\' \'{{"session_id": "sess-1", "is_error": false}}\' '
        f'> "$CLAUDE_CONFIG_DIR/{BURST_RESULT_FILENAME}"; '
        f'printf \'%s\\n\' \'{{"tool": "Read", "reason": "outside the clone"}}\' '
        f'> "$CLAUDE_CONFIG_DIR/{VIOLATIONS_FILENAME}"'
    )
    rig.runner.materialize(spec, None)
    assert rig.runner.run(spec) == 0

    artifacts = rig.runner.collect(spec, archive_as=None)
    assert artifacts.result == {"session_id": "sess-1", "is_error": False}
    assert artifacts.violations == [{"tool": "Read", "reason": "outside the clone"}]
    assert artifacts.session_key is None and artifacts.session_checksum is None


def test_collect_archives_the_volume_under_the_key_it_is_given(rig, tmp_path):
    spec = rig.burst("true")
    rig.runner.materialize(spec, None)
    _write(rig, spec, "projects/sess.jsonl", "this burst's turn\n")

    key = "sessions/owner/product#42/b1/session.tar.gz"
    artifacts = rig.runner.collect(spec, archive_as=key)
    assert artifacts.session_key == key and len(artifacts.session_checksum) == 64

    back = tmp_path / "back"
    rig.archive.get(key, back, artifacts.session_checksum)
    assert (back / "projects" / "sess.jsonl").read_text(encoding="utf-8") == "this burst's turn\n"


def test_a_burst_that_wrote_nothing_collects_nothing(rig):
    """A burst that died before the SDK came up: no result, no violations, and
    the volume is still archived if the caller asked for it — an empty snapshot
    is a fact about that burst."""
    spec = rig.burst("true")
    rig.runner.materialize(spec, None)
    artifacts = rig.runner.collect(spec, archive_as="sessions/owner/product#42/b1/session.tar.gz")
    assert artifacts.result is None and artifacts.violations == []
    assert artifacts.session_key is not None


# --- run: the exit code, the output ---


def test_the_exit_code_is_the_bursts_own(rig, tmp_path):
    assert rig.runner.run(rig.burst("echo dying; exit 3"), log_path=tmp_path / "b1.log") == 3
    # and on the plain path — no log, no heartbeat — just the same
    assert rig.runner.run(rig.burst("exit 4", burst_id="b2")) == 4


def test_output_is_teed_to_the_log_file(rig, tmp_path, capsys):
    """The transcript on the volume only exists once the SDK is up; the log file
    catches everything, including a burst that dies before writing any result."""
    log_path = tmp_path / "logs" / "b1.log"  # the directory is the runner's to make
    code = rig.runner.run(rig.burst("echo agent-line; echo oops 1>&2"), log_path=log_path)
    assert code == 0
    logged = log_path.read_text(encoding="utf-8")
    assert "agent-line" in logged
    assert "oops" in logged  # stderr too
    assert "agent-line" in capsys.readouterr().out  # and it still streams live


# --- run: the heartbeat and the fence ---


def test_the_heartbeat_is_asked_on_the_clock_not_on_output(rig, tmp_path):
    """A burst that prints nothing must not look dead (finding #6: an 18-minute
    silent Bash call). The callback is asked every poll interval whether or not
    a line arrived."""
    beats = []
    log_path = tmp_path / "b1.log"
    code = rig.runner.run(
        rig.burst("sleep 0.4; echo finally"), log_path=log_path, heartbeat=lambda: beats.append(1) or True
    )
    assert code == 0
    assert len(beats) >= 3  # asked repeatedly during the silence
    assert log_path.read_text(encoding="utf-8") == "finally\n"  # and the output still lands


def test_a_false_heartbeat_fences_a_burst_that_would_not_end_on_its_own(rig, tmp_path):
    """False means the lease is gone — a peer holds the task, or the org is
    stopped — and work nobody holds a lease on must not keep pushing. The
    runner ends the burst; `run` returns the burst's own exit, which is not
    success; what it printed before the fence is kept."""
    answers = iter([True, False])
    log_path = tmp_path / "b1.log"
    # left alone this runs four seconds and exits 0 — so an unfenced burst is a
    # failed assertion, never a hung suite
    script = "echo started; i=0; while [ $i -lt 200 ]; do sleep 0.02; i=$((i+1)); done; echo unfenced"
    code = rig.runner.run(rig.burst(script), log_path=log_path, heartbeat=lambda: next(answers, False))
    assert code != 0
    assert log_path.read_text(encoding="utf-8") == "started\n"


# --- alive: the reconciler's question ---


def test_alive_is_proof_of_life_while_the_burst_runs_and_nothing_after(rig, tmp_path):
    spec = rig.burst("sleep 0.3")
    seen = []

    def beat():
        seen.append(rig.runner.alive(spec.burst_id))  # asked from inside the burst's own run
        return True

    assert rig.runner.alive(spec.burst_id) is False  # never started
    assert rig.runner.run(spec, log_path=tmp_path / "b1.log", heartbeat=beat) == 0
    assert seen and all(seen)  # alive every time it was asked while running
    assert rig.runner.alive(spec.burst_id) is False  # and not once it has ended
    assert rig.runner.alive("never-ran") is False


# --- the box's contract ---


def test_the_box_is_given_the_burst_env_verbatim(rig, monkeypatch):
    """`burst_env` decides what enters the box — the one credential, the
    telemetry, the sandbox refs — and a runner packages it, adding nothing and
    dropping nothing. The rules themselves are tested once, in test_runner.py;
    here every runner is held to carrying them unchanged."""
    monkeypatch.setenv(agentenv.CLAUDE_CODE_OAUTH_TOKEN, "oat-test")
    spec = _spec(
        prompt="fix the bug",
        model="claude-sonnet-5",
        resume_session_id="sess-9",
        max_budget_usd=2.5,
        max_turns=40,
        traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        github_token="ghs_x",
        git_user_name="bot[bot]",
        git_user_email="b@nr.gh",
        extra_env={"SANDBOX_URL": "http://sandbox"},
    )
    expected = burst_env(spec)
    assert agentenv.CLAUDE_CODE_OAUTH_TOKEN in expected  # the spec was worth comparing
    assert rig.packaged_env(spec) == expected
