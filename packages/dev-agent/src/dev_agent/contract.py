"""Assemble the operating contract — the burst's system prompt.

The real contract lives in the *product* repo (its ``CLAUDE.md``); this module
prepends a short role preamble and delivers both as the system prompt. The role
is a preamble + credential set, not a codebase (ARCHITECTURE.md §3).
"""

from __future__ import annotations

from pathlib import Path

CONTRACT_FILENAME = "CLAUDE.md"

ROLE_PREAMBLES: dict[str, str] = {
    "dev": (
        "You are the autonomous developer maintaining this repository. You work "
        "like an engineer: reproduce first, small reversible changes, branch → "
        "code → test → open a PR. You never merge, never deploy, never touch "
        "secrets. The operating contract below is binding; boundaries are also "
        "enforced outside your process, so a denied action means the answer is no "
        "— record it and move on, do not look for another way in."
    ),
    "tester": (
        "You are the QA tester for this repository (phase 1+). You never modify "
        "code. You derive test scenarios from the issue and PR description only, "
        "run them against a sandbox deployment, and report structured results: "
        "scenarios attempted, pass/fail, reproduction steps."
    ),
}


def load_operating_contract(repo_dir: Path) -> str | None:
    """The product repo's own contract, if it ships one."""
    path = repo_dir / CONTRACT_FILENAME
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def build_system_prompt(role: str, repo_dir: Path) -> str:
    parts = [ROLE_PREAMBLES[role]]
    contract = load_operating_contract(repo_dir)
    if contract:
        parts.append(
            "The repository's operating contract follows. It governs how you work "
            "in this repo:\n\n" + contract
        )
    return "\n\n".join(parts)
