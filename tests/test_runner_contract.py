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
  for ``spec``, read back from whatever the runner packages it into.

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
from dev_agent import env as agentenv

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

    def __init__(self, piddir: Path):
        self.piddir = piddir
        self.scripts: dict[str, str] = {}

    def build_command(self, spec: BurstSpec) -> list[str]:
        # the shell writes its own pid first, so `kill` and `alive` can find it
        # the way docker finds a container by name
        pidfile = self.piddir / f"burst-{spec.burst_id}"
        return ["sh", "-c", f'echo $$ > "$0"; {self.scripts[spec.burst_id]}', str(pidfile)]

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
        self.runner = ShellDocker(tmp_path / "pids")
        self.runner.piddir.mkdir()

    def burst(self, script: str, burst_id: str = "b1") -> BurstSpec:
        self.runner.scripts[burst_id] = script
        return _spec(burst_id=burst_id)

    def packaged_env(self, spec: BurstSpec) -> dict[str, str]:
        # the real command, not the shell's: the `-e KEY=VALUE` pairs docker would be given
        cmd = DockerRunner().build_command(spec)
        pairs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-e"]
        return dict(pair.split("=", 1) for pair in pairs)


@pytest.fixture(params=RUNNERS)
def rig(request, tmp_path, monkeypatch):
    """A rig per runner. The heartbeat clock is sped up for the suite — the
    contract is *at least this often*, and the value is the runner module's."""
    monkeypatch.setattr(runner_mod, "HEARTBEAT_POLL_S", 0.05)
    if request.param == "docker":
        return DockerRig(tmp_path)
    raise AssertionError(f"no rig for {request.param!r}")


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
