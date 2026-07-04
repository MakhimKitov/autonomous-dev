"""The E2E test policy, the product manifest, and the report contract (TESTER.md)."""

import pytest
from control_plane.testing import (
    ManifestError,
    ReportError,
    E2EPolicy,
    E2EPolicyError,
    format_test_report,
    parse_manifest,
    parse_report,
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


def test_policy_ships_off():
    assert E2EPolicy.from_env({}) == E2EPolicy()
    assert E2EPolicy.from_env({"E2E": "off"}).enabled is False


def test_policy_on_with_optional_model():
    policy = E2EPolicy.from_env({"E2E": "on", "TESTER_MODEL": "claude-x"})
    assert policy.enabled and policy.tester_model == "claude-x"
    assert E2EPolicy.from_env({"E2E": "on"}).tester_model is None


def test_policy_rejects_anything_but_on_off():
    with pytest.raises(E2EPolicyError, match="on|off"):
        E2EPolicy.from_env({"E2E": "yes"})


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


def test_resolve_injects_only_the_test_prefixed_intersection():
    manifest = parse_manifest(MANIFEST)
    resolved, missing = resolve_test_env(
        manifest, {"TEST_DISCORD_TOKEN": "tok", "TEST_GUILD_ID": "1", "DISCORD_TOKEN": "prod!"}
    )
    assert resolved == {"DISCORD_TOKEN": "tok", "GUILD_ID": "1"}  # unprefixed on the way in
    assert missing == []


def test_resolve_reports_missing_by_platform_name():
    manifest = parse_manifest(MANIFEST)
    resolved, missing = resolve_test_env(manifest, {"TEST_DISCORD_TOKEN": "tok"})
    assert resolved == {"DISCORD_TOKEN": "tok"}
    assert missing == ["TEST_GUILD_ID"]  # named as the operator must provision it


def test_manifest_cannot_name_its_way_past_the_prefix():
    # a hostile manifest asking for the platform's own secrets gets nothing:
    # lookup is TEST_<name>, so only deliberately provisioned values exist
    manifest = parse_manifest("---\nenv: [CLAUDE_CODE_OAUTH_TOKEN]\n---\nprose")
    resolved, missing = resolve_test_env(manifest, {"CLAUDE_CODE_OAUTH_TOKEN": "the-real-one"})
    assert resolved == {}
    assert missing == ["TEST_CLAUDE_CODE_OAUTH_TOKEN"]


# --- the report contract ---


def test_parse_report_full_shape():
    report = parse_report(
        '{"verdict": "Solid.", "scenarios": [{"name": "boot", "result": "pass", '
        '"evidence": "synced in 4s"}], "defects": [{"summary": "crash", "repro": "run x"}]}'
    )
    assert report.verdict == "Solid."
    assert report.scenarios == [{"name": "boot", "result": "pass", "evidence": "synced in 4s"}]
    assert report.defects == [{"summary": "crash", "repro": "run x"}]


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


def test_format_test_report_wears_the_uniform():
    report = parse_report(
        '{"verdict": "One defect.", "scenarios": [{"name": "boot", "result": "pass", '
        '"evidence": "ok"}, {"name": "roll", "result": "fail", "evidence": "wrong reply"}], '
        '"defects": [{"summary": "bad format", "repro": "/roll 2d6"}]}'
    )
    body = format_test_report(report, "abc1234def", "claude-sonnet-5")
    lines = body.splitlines()
    assert lines[0] == "#### 🧪 Machine test report"
    assert lines[1] == "<sub>`claude-sonnet-5` · tested `abc1234`</sub>"
    assert "| boot | ✅ pass | ok |" in body
    assert "| roll | ❌ fail | wrong reply |" in body
    assert "- **bad format** — /roll 2d6" in body
