"""Review policy: a toggle and a selector, everything declared (REVIEWERS.md)."""

import pytest
from control_plane.github_api import COPILOT_REVIEWER
from control_plane.review import (
    FindingsError,
    ReviewPolicy,
    ReviewPolicyError,
    format_review_body,
    parse_findings,
)


def test_ships_off():
    policy = ReviewPolicy.from_env({})
    assert not policy.enabled
    assert not policy.copilot
    assert policy.machine_reviewers() == frozenset()
    assert policy.describe() == "off"


def test_off_ignores_selector():
    # a curated provider list survives being switched off
    policy = ReviewPolicy.from_env({"REVIEW": "off", "REVIEW_PROVIDERS": "copilot"})
    assert not policy.enabled and not policy.copilot


def test_on_with_empty_selector_refuses():
    with pytest.raises(ReviewPolicyError, match="at least one"):
        ReviewPolicy.from_env({"REVIEW": "on"})


def test_bad_toggle_value_refuses():
    with pytest.raises(ReviewPolicyError, match="`on` or `off`"):
        ReviewPolicy.from_env({"REVIEW": "yes"})


def test_unknown_provider_refuses():
    with pytest.raises(ReviewPolicyError, match="unknown review provider"):
        ReviewPolicy.from_env({"REVIEW": "on", "REVIEW_PROVIDERS": "chatgpt"})


def test_unshipped_provider_refuses():
    with pytest.raises(ReviewPolicyError, match="not shipped yet"):
        ReviewPolicy.from_env({"REVIEW": "on", "REVIEW_PROVIDERS": "codex"})


def test_claude_without_reviewer_app_refuses():
    with pytest.raises(ReviewPolicyError, match="REVIEWER_GITHUB_APP_ID"):
        ReviewPolicy.from_env({"REVIEW": "on", "REVIEW_PROVIDERS": "claude"})


def test_claude_declared_and_credentialed():
    policy = ReviewPolicy.from_env(
        {
            "REVIEW": "on",
            "REVIEW_PROVIDERS": "claude",
            "REVIEWER_GITHUB_APP_ID": "42",
            "REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH": "/tmp/key.pem",
        }
    )
    assert policy.claude and not policy.copilot
    # the reviewer App's bot login isn't knowable statically — the scheduler
    # adds it to the registry once learned at startup
    assert policy.machine_reviewers() == frozenset()


def test_copilot_without_pat_refuses():
    # declared-but-impossible is a startup error, not a silent degrade
    with pytest.raises(ReviewPolicyError, match="GITHUB_COPILOT_REVIEW_TOKEN"):
        ReviewPolicy.from_env({"REVIEW": "on", "REVIEW_PROVIDERS": "copilot"})


def test_copilot_declared_and_credentialed():
    policy = ReviewPolicy.from_env(
        {"REVIEW": "on", "REVIEW_PROVIDERS": "copilot", "GITHUB_COPILOT_REVIEW_TOKEN": "ghp_x"}
    )
    assert policy.enabled and policy.copilot
    assert policy.machine_reviewers() == frozenset({COPILOT_REVIEWER})
    assert policy.describe() == "on · providers=copilot"


def test_selector_is_trimmed_and_case_insensitive():
    policy = ReviewPolicy.from_env(
        {"REVIEW": "on", "REVIEW_PROVIDERS": " Copilot , ", "GITHUB_COPILOT_REVIEW_TOKEN": "ghp_x"}
    )
    assert policy.providers == ("copilot",)


# --- the reviewer burst's findings contract ---


def test_parse_findings_plain_json():
    parsed = parse_findings(
        '{"verdict": "Sound change.", "findings": [{"path": "bot/x.py", "line": 7, "body": "off-by-one"}]}'
    )
    assert parsed.verdict == "Sound change."
    assert parsed.findings == [{"path": "bot/x.py", "line": 7, "body": "off-by-one"}]


def test_parse_findings_accepts_fenced_block_and_file_level():
    parsed = parse_findings(
        'Here is my review:\n```json\n{"verdict": "ok", "findings": [{"path": "specs/a.md", "body": "scope creep"}]}\n```'
    )
    assert parsed.findings == [{"path": "specs/a.md", "line": None, "body": "scope creep"}]


def test_parse_findings_empty_list_is_a_clean_review():
    assert parse_findings('{"verdict": "clean", "findings": []}').findings == []


def test_parse_findings_rejects_garbage():
    with pytest.raises(FindingsError):
        parse_findings("I looked at the code and it seems fine!")
    with pytest.raises(FindingsError):
        parse_findings('{"findings": []}')  # no verdict
    with pytest.raises(FindingsError):
        parse_findings('{"verdict": "x", "findings": [{"line": 3, "body": "no path"}]}')


def test_format_review_body_wears_the_uniform():
    body = format_review_body("Sound change.", "abc1234def", "claude-sonnet-4-5")
    lines = body.splitlines()
    assert lines[0] == "#### 🤖 Machine review"
    # metadata rides above the verdict — post_review may append findings below
    assert lines[1] == "<sub>`claude-sonnet-4-5` · reviewed `abc1234`</sub>"
    assert lines[-1] == "Sound change."


def test_format_review_body_without_model():
    body = format_review_body("ok", "abc1234def", None)
    assert "<sub>reviewed `abc1234`</sub>" in body and "None" not in body
