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


def test_final_text_is_the_last_assistant_message(tmp_path, monkeypatch):
    """Structured reviewer output rides the final message alone — earlier
    narration must not contaminate it."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    def _assistant(text):
        return AssistantMessage(content=[TextBlock(text=text)], model="m")

    monkeypatch.setattr(
        burst,
        "query",
        _fake_stream(
            _assistant("Let me look at the diff."),
            _assistant('{"verdict": "ok", "findings": []}'),
            ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=2,
                session_id="sess-2",
                total_cost_usd=0.01,
                usage={},
            ),
        ),
    )
    env = BurstEnv(
        task_id="5", burst_id="b2", task_prompt="review it",
        repo_dir=tmp_path, session_dir=tmp_path / "session",
    )
    result = burst.run_burst_sync(env)
    assert result.final_text == '{"verdict": "ok", "findings": []}'
    assert "Let me look" in result.text_tail  # the tail still keeps the narrative


def test_tool_use_is_logged_but_never_contaminates_final_text(tmp_path, monkeypatch, capsys):
    """A silent burst looks hung from outside — tool calls must reach the log;
    they must not leak into final_text (the structured-output channel)."""
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    monkeypatch.setattr(
        burst,
        "query",
        _fake_stream(
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git diff"})],
                model="m",
            ),
            AssistantMessage(content=[TextBlock(text='{"verdict": "ok", "findings": []}')], model="m"),
            ResultMessage(
                subtype="success", duration_ms=10, duration_api_ms=5, is_error=False,
                num_turns=2, session_id="s", total_cost_usd=0.01, usage={},
            ),
        ),
    )
    env = BurstEnv(task_id="7", burst_id="b3", task_prompt="review", repo_dir=tmp_path,
                   session_dir=tmp_path / "session")
    result = burst.run_burst_sync(env)
    out = capsys.readouterr().out
    assert '[tool] Bash {"command": "git diff"}' in out
    assert result.final_text == '{"verdict": "ok", "findings": []}'
    assert "[tool]" not in result.text_tail
