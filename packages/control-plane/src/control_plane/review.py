"""Review policy (REVIEWERS.md): a toggle and a selector, everything declared.

Machine review is on or off; when on, ``REVIEW_PROVIDERS`` names exactly who
fills the reviewer role. Nothing is inferred from credentials — a declaration
that can't be honored is a startup error, never a silent degrade.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from control_plane.github_api import COPILOT_REVIEWER, COPILOT_TOKEN_ENV

REVIEW_ENV = "REVIEW"
PROVIDERS_ENV = "REVIEW_PROVIDERS"
REVIEWER_MODEL_ENV = "REVIEWER_MODEL"
REVIEWER_APP_PREFIX = "REVIEWER_"  # REVIEWER_GITHUB_APP_ID / _PRIVATE_KEY_PATH

RECOGNIZED = ("claude", "codex", "copilot")
IMPLEMENTED = ("claude", "copilot")  # codex arrives with REVIEWERS.md slice 3


class ReviewPolicyError(RuntimeError):
    """The declared review policy can't be honored — refuse to start."""


@dataclass(slots=True, frozen=True)
class ReviewPolicy:
    enabled: bool = False  # ships off: turning review on is the operator's explicit act
    providers: tuple[str, ...] = ()
    reviewer_model: str | None = None  # the claude provider's burst model (slice 2)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ReviewPolicy:
        env = dict(os.environ) if environ is None else environ
        toggle = env.get(REVIEW_ENV, "").strip().lower() or "off"
        if toggle not in ("on", "off"):
            raise ReviewPolicyError(f"{REVIEW_ENV} must be `on` or `off`, got {toggle!r}")
        if toggle == "off":
            return cls()  # off ignores the selector — a curated list survives the toggle
        providers = tuple(p.strip().lower() for p in env.get(PROVIDERS_ENV, "").split(",") if p.strip())
        if not providers:
            raise ReviewPolicyError(
                f"{REVIEW_ENV}=on needs {PROVIDERS_ENV} to name at least one of: {', '.join(RECOGNIZED)}"
            )
        for provider in providers:
            if provider not in RECOGNIZED:
                raise ReviewPolicyError(
                    f"unknown review provider {provider!r} — recognized: {', '.join(RECOGNIZED)}"
                )
            if provider not in IMPLEMENTED:
                raise ReviewPolicyError(
                    f"review provider {provider!r} is not shipped yet (REVIEWERS.md slices) — "
                    f"available today: {', '.join(IMPLEMENTED)}"
                )
        if "copilot" in providers and not env.get(COPILOT_TOKEN_ENV, "").strip():
            raise ReviewPolicyError(
                f"provider `copilot` declared but {COPILOT_TOKEN_ENV} is unset — GitHub grants "
                "Copilot review requests only to a user token whose owner holds a Copilot seat"
            )
        if "claude" in providers and not (
            env.get("REVIEWER_GITHUB_APP_ID", "").strip()
            and env.get("REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
        ):
            raise ReviewPolicyError(
                "provider `claude` declared but the reviewer App is not configured — set "
                "REVIEWER_GITHUB_APP_ID and REVIEWER_GITHUB_APP_PRIVATE_KEY_PATH (the PR's "
                "author can't review it, so findings post as a second App)"
            )
        return cls(
            enabled=True,
            providers=providers,
            reviewer_model=env.get(REVIEWER_MODEL_ENV, "").strip() or None,
        )

    @property
    def copilot(self) -> bool:
        return self.enabled and "copilot" in self.providers

    @property
    def claude(self) -> bool:
        return self.enabled and "claude" in self.providers

    def machine_reviewers(self) -> frozenset[str]:
        """The registry: bot logins whose inline findings trigger dev review rounds."""
        logins: set[str] = set()
        if self.copilot:
            logins.add(COPILOT_REVIEWER)
        return frozenset(logins)

    def describe(self) -> str:
        """The startup switchboard line, next to the rails."""
        if not self.enabled:
            return "off"
        return "on · providers=" + ", ".join(self.providers)


@dataclass(slots=True)
class ReviewFindings:
    """A reviewer burst's structured verdict, parsed from its final message."""

    verdict: str
    findings: list[dict]  # {path, line, body} — line may be None (file-level)


class FindingsError(ValueError):
    """The reviewer burst's final message wasn't the contracted JSON."""


def parse_findings(final_text: str) -> ReviewFindings:
    """The reviewer's contract: the final message IS a JSON object
    ``{"verdict": str, "findings": [{"path", "line", "body"}]}``. Accept a
    fenced code block around it; anything else is a failed review burst."""
    text = (final_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise FindingsError("no JSON object in the reviewer's final message")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise FindingsError(f"reviewer JSON does not parse: {exc}") from exc
    verdict = str(data.get("verdict") or "").strip()
    if not verdict:
        raise FindingsError("reviewer JSON has no verdict")
    findings = []
    for item in data.get("findings") or []:
        path, body = str(item.get("path") or "").strip(), str(item.get("body") or "").strip()
        if not path or not body:
            raise FindingsError(f"finding missing path or body: {item!r}")
        line = item.get("line")
        findings.append({"path": path, "line": int(line) if line else None, "body": body})
    return ReviewFindings(verdict=verdict, findings=findings)


def format_review_body(verdict: str, head_sha: str, model: str | None) -> str:
    """The posted review's overview: the verdict in uniform. The metadata line
    sits *above* the verdict because ``post_review`` may append file-level
    findings below — and it pins the reviewed commit, which is what makes a
    multi-round PR's review history legible."""
    meta = " · ".join(
        part for part in (f"`{model}`" if model else None, f"reviewed `{head_sha[:7]}`") if part
    )
    return f"#### 🤖 Machine review\n<sub>{meta}</sub>\n\n{verdict}"
