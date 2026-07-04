"""Operating-contract assembly (system prompt)."""

from dev_agent.contract import ROLE_PREAMBLES, build_system_prompt


def test_role_preamble_without_repo_contract(tmp_path):
    prompt = build_system_prompt("dev", tmp_path)
    assert prompt == ROLE_PREAMBLES["dev"]


def test_repo_contract_is_appended(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Rules\nNever deploy.", encoding="utf-8")
    prompt = build_system_prompt("dev", tmp_path)
    assert prompt.startswith(ROLE_PREAMBLES["dev"])
    assert "Never deploy." in prompt


def test_tester_role_exists(tmp_path):
    assert "never modify" in build_system_prompt("tester", tmp_path).lower()


def test_reviewer_role_gets_contract_as_rubric_not_orders(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Rules\nNever deploy.", encoding="utf-8")
    prompt = build_system_prompt("reviewer", tmp_path)
    assert "independent code reviewer" in prompt
    assert "no GitHub" in prompt
    assert "review criteria" in prompt  # the dev's contract judges the diff...
    assert "does not direct your own actions" in prompt  # ...it doesn't drive the reviewer
    assert "Never deploy." in prompt
