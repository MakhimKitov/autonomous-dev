"""The container entrypoint — what it binds before it does anything else."""

import structlog
from dev_agent import cli
from dev_agent import env as agentenv


def test_the_correlation_ids_are_bound_before_the_command_runs(monkeypatch):
    """Every line this process writes has to carry the ids (TO-BE §12), and its
    stdout is teed to `data/logs/<burst_id>.log` — often the only record of a burst
    a reader has, and the one they reach for when the trace backend they need is not
    the one that was running. The ids come from the environment because that is the
    only place the container can learn them.
    """
    structlog.contextvars.clear_contextvars()
    seen: dict[str, str] = {}

    def capture(env):
        seen.update(structlog.contextvars.get_contextvars())
        return 0

    monkeypatch.setattr(cli, "_check", capture)
    monkeypatch.setenv(agentenv.TASK_ID, "owner/product#42")
    monkeypatch.setenv(agentenv.BURST_ID, "b9")
    monkeypatch.setenv(agentenv.TRACEPARENT, f"00-{'a' * 32}-{'b' * 16}-01")
    try:
        assert cli.main(["check"]) == 0
    finally:
        structlog.contextvars.clear_contextvars()

    assert seen == {"task_id": "owner/product#42", "burst_id": "b9", "trace_id": "a" * 32}


def test_an_untraced_burst_still_binds_what_it_knows(monkeypatch):
    """Tracing off is a normal state — a hand-run burst, an image without an
    endpoint. `trace_id` is then absent rather than empty: a key whose value says
    nothing is worse in logfmt than no key, because it reads as a lost trace."""
    structlog.contextvars.clear_contextvars()
    seen: dict[str, str] = {}

    def capture(env):
        seen.update(structlog.contextvars.get_contextvars())
        return 0

    monkeypatch.setattr(cli, "_check", capture)
    monkeypatch.setenv(agentenv.TASK_ID, "owner/product#42")
    monkeypatch.setenv(agentenv.BURST_ID, "b9")
    monkeypatch.delenv(agentenv.TRACEPARENT, raising=False)
    try:
        assert cli.main(["check"]) == 0
    finally:
        structlog.contextvars.clear_contextvars()

    assert seen == {"task_id": "owner/product#42", "burst_id": "b9"}
