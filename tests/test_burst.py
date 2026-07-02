"""The burst loop's result assembly, driven by a faked SDK message stream."""

import json

from claude_agent_sdk import ResultMessage, SystemMessage
from dev_agent import burst
from dev_agent.env import BurstEnv


def _fake_stream(*messages):
    async def fake_query(*, prompt, options):
        for message in messages:
            yield message

    return fake_query


def test_result_records_the_resolved_model(tmp_path, monkeypatch):
    """The init message names the model the CLI actually resolved — that, not
    the requested alias, is what lands in the burst result."""
    monkeypatch.setattr(
        burst,
        "query",
        _fake_stream(
            SystemMessage(subtype="init", data={"model": "claude-sonnet-5-20260203"}),
            ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id="sess-1",
                total_cost_usd=0.01,
                usage={"input_tokens": 3, "output_tokens": 4},
            ),
        ),
    )
    env = BurstEnv(
        task_id="42",
        burst_id="b1",
        task_prompt="do the thing",
        repo_dir=tmp_path,
        session_dir=tmp_path / "session",
        model="sonnet",
    )
    result = burst.run_burst_sync(env)

    assert result.model == "claude-sonnet-5-20260203"
    assert result.session_id == "sess-1"
    assert result.num_turns == 1
    written = json.loads(env.result_path.read_text(encoding="utf-8"))
    assert written["model"] == "claude-sonnet-5-20260203"
