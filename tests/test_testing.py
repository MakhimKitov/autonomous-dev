"""The E2E test policy, the product manifest, and the report contract (TESTER.md)."""

import json
import tempfile
from pathlib import Path

import pytest
from control_plane.models import RepositoryRecord
from control_plane.secrets import SecretResolver
from control_plane.testing import (
    CitationError,
    ManifestError,
    ReportError,
    E2EPolicy,
    E2EPolicyError,
    format_test_report,
    parse_manifest,
    parse_report,
    resolve_citations,
    resolve_test_env,
)

MANIFEST = """\
---
env: [DISCORD_TOKEN, GUILD_ID]
tools: []
---

# E2E testing contract

Boot: `uv run python -m bot`.
"""


# --- policy ---

TESTER_CREDS = {
    "TESTER_GITHUB_APP_ID": "77",
    "TESTER_GITHUB_APP_PRIVATE_KEY_PATH": "/keys/tester.pem",
}



def test_policy_ships_off():
    assert E2EPolicy.from_env({}) == E2EPolicy()
    assert E2EPolicy.from_env({"E2E": "off"}).enabled is False


def test_policy_on_with_optional_model():
    policy = E2EPolicy.from_env(dict(TESTER_CREDS, E2E="on", TESTER_MODEL="claude-x"))
    assert policy.enabled and policy.tester_model == "claude-x"
    assert E2EPolicy.from_env(dict(TESTER_CREDS, E2E="on")).tester_model is None


def test_policy_on_without_the_tester_app_refuses():
    # D3: reports post under the tester App's own identity — e2e declared
    # without that App is a startup error, never a silent degrade
    with pytest.raises(E2EPolicyError, match="TESTER_GITHUB_APP_ID"):
        E2EPolicy.from_env({"E2E": "on"})
    with pytest.raises(E2EPolicyError, match="tester App"):
        E2EPolicy.from_env({"E2E": "on", "TESTER_GITHUB_APP_ID": "77"})
    # off never asks the credential question
    assert E2EPolicy.from_env({"E2E": "off"}).enabled is False


def test_policy_rejects_anything_but_on_off():
    with pytest.raises(E2EPolicyError, match="on|off"):
        E2EPolicy.from_env({"E2E": "yes"})


def test_policy_from_product_row_governs():
    # C3: the row overrides the machine's E2E toggle in both directions
    on = RepositoryRecord(repository="fermata/discord-bot", team="platform", e2e_enabled=True)
    off = RepositoryRecord(repository="fermata/discord-bot", team="platform")
    assert E2EPolicy.from_product(on, dict(TESTER_CREDS, E2E="off")).enabled is True
    assert E2EPolicy.from_product(off, {"E2E": "on"}).enabled is False


def test_policy_from_product_carries_the_rows_refs():
    # D4: the row's test_env_refs are the declaration the tester can reach;
    # an env-governed policy carries None — the TEST_ prefix default
    refs = {"DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN", "GUILD_ID": "file:/run/secrets/guild"}
    row = RepositoryRecord(
        repository="fermata/discord-bot", team="platform", e2e_enabled=True, test_env_refs=refs
    )
    policy = E2EPolicy.from_product(row, dict(TESTER_CREDS))
    assert policy.env_refs == refs and policy.env_refs is not row.test_env_refs
    assert E2EPolicy.from_env(dict(TESTER_CREDS, E2E="on")).env_refs is None


def test_policy_from_product_model_stays_a_machine_answer():
    row = RepositoryRecord(repository="fermata/discord-bot", team="platform", e2e_enabled=True)
    policy = E2EPolicy.from_product(row, dict(TESTER_CREDS, TESTER_MODEL="claude-haiku-4-5"))
    assert policy.tester_model == "claude-haiku-4-5"
    env = dict(TESTER_CREDS, E2E="off")
    E2EPolicy.from_product(row, env)
    assert env == dict(TESTER_CREDS, E2E="off")  # the probe never leaks into the caller's dict


# --- manifest ---


def test_manifest_parses_env_tools_and_prose():
    manifest = parse_manifest(MANIFEST)
    assert manifest.env == ("DISCORD_TOKEN", "GUILD_ID")
    assert manifest.tools == ()
    assert manifest.prose.startswith("# E2E testing contract")


def test_manifest_requires_frontmatter():
    with pytest.raises(ManifestError, match="frontmatter"):
        parse_manifest("# just prose\n")
    with pytest.raises(ManifestError, match="never closes"):
        parse_manifest("---\nenv: []\n")


def test_manifest_rejects_unknown_keys_and_shapes():
    with pytest.raises(ManifestError, match="unknown line"):
        parse_manifest("---\nsecrets: [X]\n---\n")
    with pytest.raises(ManifestError, match="flow, list"):
        parse_manifest("---\nenv: DISCORD_TOKEN\n---\n")


def test_manifest_rejects_devious_env_names():
    # the manifest is untrusted — only boring env var names pass
    with pytest.raises(ManifestError, match="not a plain env var name"):
        parse_manifest("---\nenv: [x; rm -rf /]\n---\n")


def test_manifest_rejects_unshipped_tools():
    with pytest.raises(ManifestError, match="not shipped"):
        parse_manifest("---\nenv: []\ntools: [playwright]\n---\n")


# --- the intersection ---


def test_env_governed_refs_are_the_test_prefix():
    # the unregistered default: every manifest name X reads env:TEST_X — the
    # MVP's allowlist boundary, spelled as refs (D4)
    manifest = parse_manifest(MANIFEST)
    assert E2EPolicy(enabled=True).refs_for(manifest) == {
        "DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN",
        "GUILD_ID": "env:TEST_GUILD_ID",
    }


def test_registered_refs_intersect_with_the_ask():
    # a registered product's declaration, intersected: a name the row lacks is
    # absent (not defaulted), a ref the manifest never asks for is not offered
    manifest = parse_manifest(MANIFEST)
    policy = E2EPolicy(
        enabled=True,
        env_refs={"DISCORD_TOKEN": "helper:discord/test-token", "STALE": "env:TEST_STALE"},
    )
    assert policy.refs_for(manifest) == {"DISCORD_TOKEN": "helper:discord/test-token"}


def test_resolve_injects_only_the_declared_intersection():
    manifest = parse_manifest(MANIFEST)
    resolver = SecretResolver(
        {"TEST_DISCORD_TOKEN": "tok", "TEST_GUILD_ID": "1", "DISCORD_TOKEN": "prod!"}
    )
    resolved, missing = resolve_test_env(
        manifest, E2EPolicy(enabled=True).refs_for(manifest), resolver
    )
    assert resolved == {"DISCORD_TOKEN": "tok", "GUILD_ID": "1"}  # unprefixed on the way in
    assert missing == []


def test_resolve_reports_missing_with_the_ref_and_why():
    manifest = parse_manifest(MANIFEST)
    refs = {"DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN"}  # GUILD_ID never declared
    resolver = SecretResolver({})
    resolved, missing = resolve_test_env(manifest, refs, resolver)
    assert resolved == {}
    assert missing == [
        "DISCORD_TOKEN ← env:TEST_DISCORD_TOKEN (env var TEST_DISCORD_TOKEN is unset)",
        "GUILD_ID ← no ref declared",
    ]


def test_resolve_reads_file_refs_at_spawn(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("from-a-file\n", encoding="utf-8")
    manifest = parse_manifest("---\nenv: [DISCORD_TOKEN]\n---\nprose")
    resolved, missing = resolve_test_env(
        manifest, {"DISCORD_TOKEN": f"file:{token_file}"}, SecretResolver({})
    )
    assert (resolved, missing) == ({"DISCORD_TOKEN": "from-a-file"}, [])


def test_manifest_cannot_name_its_way_past_the_declaration():
    # a hostile manifest asking for the platform's own secrets gets nothing,
    # in both modes: env-governed looks up TEST_<name>; registered offers only
    # what the row declares
    manifest = parse_manifest("---\nenv: [CLAUDE_CODE_OAUTH_TOKEN]\n---\nprose")
    resolver = SecretResolver({"CLAUDE_CODE_OAUTH_TOKEN": "the-real-one"})
    resolved, missing = resolve_test_env(
        manifest, E2EPolicy(enabled=True).refs_for(manifest), resolver
    )
    assert resolved == {}
    assert missing == [
        "CLAUDE_CODE_OAUTH_TOKEN ← env:TEST_CLAUDE_CODE_OAUTH_TOKEN "
        "(env var TEST_CLAUDE_CODE_OAUTH_TOKEN is unset)"
    ]
    registered = E2EPolicy(enabled=True, env_refs={"DISCORD_TOKEN": "env:TEST_DISCORD_TOKEN"})
    resolved, missing = resolve_test_env(manifest, registered.refs_for(manifest), resolver)
    assert resolved == {} and missing == ["CLAUDE_CODE_OAUTH_TOKEN ← no ref declared"]


# --- the report contract ---


def test_parse_report_full_shape():
    report = parse_report(
        '{"verdict": "Solid.", "scenarios": [{"name": "boot", "result": "pass", '
        '"tool_use_id": "toolu_01A", "evidence": "synced in 4s"}], '
        '"defects": [{"summary": "crash", "repro": "run x"}]}'
    )
    assert report.verdict == "Solid."
    assert report.scenarios == [
        {"name": "boot", "result": "pass", "tool_use_id": "toolu_01A", "evidence": "synced in 4s"}
    ]
    # preexisting defaults to False: an unclassified defect belongs to the PR
    assert report.defects == [{"summary": "crash", "repro": "run x", "preexisting": False}]
    assert report.pr_caused == report.defects and report.issues == []


def test_parse_report_defect_routing_and_authored_issues():
    report = parse_report(
        '{"verdict": "x", "scenarios": [], '
        '"defects": [{"summary": "old bug", "repro": "r", "preexisting": true}, '
        '{"summary": "new bug", "repro": "r"}], '
        '"issues": [{"title": "logging broken", "body": "### Summary\\n..."}]}'
    )
    assert [d["summary"] for d in report.pr_caused] == ["new bug"]
    assert report.issues == [{"title": "logging broken", "body": "### Summary\n..."}]


def test_parse_report_rejects_issue_without_body():
    with pytest.raises(ReportError, match="title and a body"):
        parse_report('{"verdict": "x", "issues": [{"title": "no body"}]}')


def test_parse_report_accepts_fenced_and_empty_defects():
    report = parse_report('```json\n{"verdict": "ok", "scenarios": [], "defects": []}\n```')
    assert report.verdict == "ok" and report.defects == []


def test_parse_report_rejects_garbage():
    with pytest.raises(ReportError):
        parse_report("All tests passed, great PR!")
    with pytest.raises(ReportError):
        parse_report('{"scenarios": []}')  # no verdict
    with pytest.raises(ReportError, match="pass|fail"):
        parse_report('{"verdict": "x", "scenarios": [{"name": "boot", "result": "maybe"}]}')
    with pytest.raises(ReportError, match="summary"):
        parse_report('{"verdict": "x", "defects": [{"repro": "no summary"}]}')
    # B6: a scenario without a citation is malformed — prose alone asserts nothing
    with pytest.raises(ReportError, match="tool_use_id"):
        parse_report(
            '{"verdict": "x", "scenarios": [{"name": "boot", "result": "pass", '
            '"evidence": "trust me"}]}'
        )


def test_format_test_report_wears_the_uniform():
    report = parse_report(
        '{"verdict": "One defect.", "scenarios": [{"name": "boot", "result": "pass", '
        '"tool_use_id": "toolu_01A", "evidence": "ok"}, {"name": "roll", "result": "fail", '
        '"tool_use_id": "toolu_01B", "evidence": "wrong reply"}], '
        '"defects": [{"summary": "bad format", "repro": "/roll 2d6"}]}'
    )
    citations = resolve_citations(report, _volume_with(
        ("toolu_01A", "Bash", {"command": "grep boot /session/product.log"}, False),
        ("toolu_01B", "Bash", {"command": "echo roll | check"}, False),
    ))
    body = format_test_report(report, "abc1234def", "claude-sonnet-5", citations=citations)
    lines = body.splitlines()
    assert lines[0] == "#### 🧪 Machine test report"
    assert lines[1] == "<sub>`claude-sonnet-5` · tested `abc1234`</sub>"
    # the cited-call column is the platform's, extracted from the transcript
    assert "| boot | ✅ pass | `Bash: grep boot /session/product.log` | ok |" in body
    # a pipe in the recorded command must not break the table
    assert "| roll | ❌ fail | `Bash: echo roll \\| check` | wrong reply |" in body
    assert "- **bad format** — /roll 2d6" in body


# --- citation resolution (B6) ---


def _volume_with(*blocks, tmp_path=None):
    """A minimal burst volume: `projects/<slug>/sess.jsonl` shaped like the CLI's
    transcript. Each block is (tool_use_id, tool, input, is_error); is_error=None
    means the call has no recorded result (an interrupted burst)."""
    volume = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    projects = volume / "projects" / "-workspace-repo"
    projects.mkdir(parents=True, exist_ok=True)
    lines = []
    for use_id, tool, tool_input, is_error in blocks:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": use_id, "name": tool, "input": tool_input}
                        ]
                    },
                }
            )
        )
        if is_error is not None:
            lines.append(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": use_id, "is_error": is_error}
                            ]
                        },
                    }
                )
            )
    (projects / "sess.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return volume


def _report_citing(*ids):
    scenarios = ", ".join(
        f'{{"name": "s{i}", "result": "pass", "tool_use_id": "{cid}", "evidence": "e"}}'
        for i, cid in enumerate(ids)
    )
    return parse_report(f'{{"verdict": "v", "scenarios": [{scenarios}]}}')


def test_citations_resolve_to_the_recorded_call(tmp_path):
    volume = _volume_with(
        ("toolu_01A", "Bash", {"command": "grep -q 'synced'  /session/product.log"}, False),
        tmp_path=tmp_path,
    )
    resolved = resolve_citations(_report_citing("toolu_01A"), volume)
    call = resolved["toolu_01A"]
    # the platform's rendering: tool name + flattened input, never the prose
    assert call.tool == "Bash" and call.detail == "grep -q 'synced' /session/product.log"


def test_citation_missing_from_the_transcript_fails_the_report(tmp_path):
    volume = _volume_with(("toolu_01A", "Bash", {"command": "true"}, False), tmp_path=tmp_path)
    with pytest.raises(CitationError, match="toolu_ghost is not a tool call"):
        resolve_citations(_report_citing("toolu_ghost"), volume)


def test_citation_without_a_recorded_result_fails_the_report(tmp_path):
    volume = _volume_with(("toolu_01A", "Bash", {"command": "sleep 999"}, None), tmp_path=tmp_path)
    with pytest.raises(CitationError, match="no recorded result"):
        resolve_citations(_report_citing("toolu_01A"), volume)


def test_citation_of_an_errored_call_fails_the_report(tmp_path):
    """The wall's denials and crashed calls are is_error results — a scenario
    cannot stand on a call that did not succeed. The tester cites the call that
    *observed* the outcome instead."""
    volume = _volume_with(("toolu_01A", "Bash", {"command": "boom"}, True), tmp_path=tmp_path)
    with pytest.raises(CitationError, match="recorded result is an error"):
        resolve_citations(_report_citing("toolu_01A"), volume)


def test_citation_error_is_a_report_error():
    """B6's done-when: unresolvable citations fail the same way malformed JSON
    does — the scheduler's one `except ReportError` covers both."""
    assert issubclass(CitationError, ReportError)


def test_no_scenarios_resolve_to_nothing_even_without_a_volume(tmp_path):
    """A report with no scenarios has nothing to resolve — and the resolver must
    not require a transcript to say so (a missing volume yields no blocks)."""
    report = parse_report('{"verdict": "nothing ran", "scenarios": []}')
    assert resolve_citations(report, tmp_path / "no-such-volume") == {}
