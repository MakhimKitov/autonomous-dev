"""The E2E test policy and the product's testing manifest (TESTER.md).

The platform owns the envelope; the product owns the knowledge. Its
``TESTING.md`` carries YAML-ish frontmatter for the control plane (what to
inject) and prose for the tester agent (how to test). The manifest is
UNTRUSTED — it lives in a repo the dev agent writes to — so credentials are
resolved by intersection: only names the manifest asks for AND the operator
provisioned under the ``TEST_`` prefix ever enter the container.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from control_plane.models import RepositoryRecord

E2E_ENV = "E2E"
TESTER_MODEL_ENV = "TESTER_MODEL"
TEST_ENV_PREFIX = "TEST_"  # the allowlist boundary and the radioactivity marker
MANIFEST_FILENAME = "TESTING.md"

RECOGNIZED_TOOLS: tuple[str, ...] = ()  # slice 2 adds "playwright"

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")  # boring env names only — no smuggling


class E2EPolicyError(ValueError):
    """The declared E2E configuration cannot be honored — refuse to start."""


@dataclass(frozen=True, slots=True)
class E2EPolicy:
    """The operator's declaration: a toggle and an optional model override."""

    enabled: bool = False
    tester_model: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> E2EPolicy:
        env = dict(os.environ) if environ is None else environ
        raw = (env.get(E2E_ENV) or "off").strip().lower()
        if raw not in ("on", "off"):
            raise E2EPolicyError(f"{E2E_ENV} must be on|off, got {raw!r}")
        if raw == "off":
            return cls()
        if not (
            env.get("TESTER_GITHUB_APP_ID", "").strip()
            and env.get("TESTER_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
        ):
            # declared-but-unhonorable is a startup error, never a silent
            # degrade — test reports and tester-authored issues post under the
            # tester's own identity (D3), so e2e needs that App to exist here
            raise E2EPolicyError(
                "E2E=on but the tester App is not configured — set TESTER_GITHUB_APP_ID and "
                "TESTER_GITHUB_APP_PRIVATE_KEY_PATH (test reports post under the tester's "
                "own identity, not the reviewer's)"
            )
        return cls(enabled=True, tester_model=(env.get(TESTER_MODEL_ENV) or "").strip() or None)

    @classmethod
    def from_product(
        cls, row: RepositoryRecord, environ: dict[str, str] | None = None
    ) -> E2EPolicy:
        """The registered product's toggle (C3): the row overrides ``E2E``;
        ``TESTER_MODEL`` stays env-sourced — which model tests is a deployment
        fact, not product policy. Delegates to `from_env` on a probe env so
        both sources behave identically."""
        probe = dict(os.environ) if environ is None else dict(environ)
        probe[E2E_ENV] = "on" if row.e2e_enabled else "off"
        return cls.from_env(probe)

    def describe(self) -> str:
        return "on" if self.enabled else "off"


class ManifestError(ValueError):
    """The product's TESTING.md frontmatter is missing or unusable."""


@dataclass(slots=True)
class TestManifest:
    """The machine-readable half of the product's testing contract."""

    env: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    prose: str = ""  # the tester agent's operating contract, verbatim


def parse_manifest(text: str) -> TestManifest:
    """Frontmatter between ``---`` fences: ``env``/``tools`` flow lists only.

    Deliberately strict and deliberately not a YAML parser — two keys, one
    shape. Unknown keys refuse: a manifest asking for more than the socket
    offers is a contract mismatch, not something to guess around.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ManifestError(f"{MANIFEST_FILENAME} has no frontmatter (--- fence)")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        raise ManifestError(f"{MANIFEST_FILENAME} frontmatter never closes") from None
    fields: dict[str, tuple[str, ...]] = {}
    for line in lines[1:end]:
        bare = line.split("#", 1)[0].strip()
        if not bare:
            continue
        key, sep, value = bare.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or key not in ("env", "tools"):
            raise ManifestError(f"{MANIFEST_FILENAME} frontmatter: unknown line {line.strip()!r}")
        if not (value.startswith("[") and value.endswith("]")):
            raise ManifestError(f"{MANIFEST_FILENAME} frontmatter: {key} must be a [flow, list]")
        items = tuple(item.strip() for item in value[1:-1].split(",") if item.strip())
        fields[key] = items
    env = fields.get("env", ())
    for name in env:
        if not _ENV_NAME.match(name):
            raise ManifestError(f"{MANIFEST_FILENAME}: {name!r} is not a plain env var name")
    tools = fields.get("tools", ())
    for tool in tools:
        if tool not in RECOGNIZED_TOOLS:
            raise ManifestError(
                f"{MANIFEST_FILENAME}: tool {tool!r} is not shipped (TESTER.md slices) — "
                f"available today: {', '.join(RECOGNIZED_TOOLS) or 'none'}"
            )
    return TestManifest(env=env, tools=tools, prose="\n".join(lines[end + 1 :]).strip())


def resolve_test_env(
    manifest: TestManifest, environ: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """The intersection: manifest name ``X`` ← platform ``TEST_X``.

    Returns (injectable, missing). The manifest can only name its way to
    ``TEST_``-prefixed values — nothing else is reachable, by construction.
    """
    env = dict(os.environ) if environ is None else environ
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in manifest.env:
        value = (env.get(TEST_ENV_PREFIX + name) or "").strip()
        if value:
            resolved[name] = value
        else:
            missing.append(TEST_ENV_PREFIX + name)
    return resolved, missing


class ReportError(ValueError):
    """The tester burst's final message wasn't the contracted JSON."""


class CitationError(ReportError):
    """A scenario's citation does not resolve against the burst's transcript.

    A subclass on purpose: B6's contract is that an unbacked claim fails the
    report *the same way* a malformed one does — one except branch, the same
    retry rail, and nothing new blocks the task."""


@dataclass(slots=True)
class TestReport:
    """A tester burst's structured report, parsed from its final message."""

    verdict: str
    scenarios: list[dict] = field(default_factory=list)  # {name, result, evidence}
    defects: list[dict] = field(default_factory=list)  # {summary, repro, preexisting}
    issues: list[dict] = field(default_factory=list)  # {title, body} — tester-authored

    @property
    def pr_caused(self) -> list[dict]:
        """Defects the diff introduced — they go back to the developer."""
        return [d for d in self.defects if not d.get("preexisting")]


def parse_report(final_text: str) -> TestReport:
    """The tester's contract: the final message IS a JSON object
    ``{"verdict", "scenarios": [{"name", "result", "evidence"}],
    "defects": [{"summary", "repro", "preexisting"}], "issues": [{"title",
    "body"}]}``. Accept a fenced block around it. ``issues`` are the complete
    symptom reports the tester authored for pre-existing defects — the
    platform relays them verbatim (authorship stays with the agent)."""
    text = (final_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ReportError("no JSON object in the tester's final message")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReportError(f"tester JSON does not parse: {exc}") from exc
    verdict = str(data.get("verdict") or "").strip()
    if not verdict:
        raise ReportError("tester JSON has no verdict")
    scenarios = []
    for item in data.get("scenarios") or []:
        name = str(item.get("name") or "").strip()
        result = str(item.get("result") or "").strip().lower()
        if not name or result not in ("pass", "fail"):
            raise ReportError(f"scenario needs a name and result pass|fail: {item!r}")
        # B6: the citation is part of the shape. Prose stays optional — it is the
        # tester's own words; the tool_use_id is what the platform can check.
        cited = str(item.get("tool_use_id") or "").strip()
        if not cited:
            raise ReportError(f"scenario cites no tool_use_id: {item!r}")
        scenarios.append(
            {
                "name": name,
                "result": result,
                "tool_use_id": cited,
                "evidence": str(item.get("evidence") or "").strip(),
            }
        )
    defects = []
    for item in data.get("defects") or []:
        summary = str(item.get("summary") or "").strip()
        if not summary:
            raise ReportError(f"defect missing summary: {item!r}")
        defects.append(
            {
                "summary": summary,
                "repro": str(item.get("repro") or "").strip(),
                "preexisting": bool(item.get("preexisting")),
            }
        )
    issues = []
    for item in data.get("issues") or []:
        title = str(item.get("title") or "").strip()
        body = str(item.get("body") or "").strip()
        if not title or not body:
            raise ReportError(f"issue needs a title and a body: {item!r}")
        issues.append({"title": title, "body": body})
    return TestReport(verdict=verdict, scenarios=scenarios, defects=defects, issues=issues)


@dataclass(frozen=True, slots=True)
class CitedCall:
    """What the transcript records for one cited tool_use_id.

    The platform's column in the posted table: the call and its recorded
    outcome, extracted from the archived transcript — never from the report.
    """

    tool: str
    detail: str  # compact rendering of the call's input (a command, a path)


def _transcript_tool_blocks(volume_dir: Path):
    """Yield every tool_use / tool_result block in this burst's transcript.

    The CLI writes `projects/<cwd-slug>/<session_id>.jsonl` on the session
    volume. A tester volume is never seeded (TESTER.md), so everything under
    `projects/` was recorded by *this* burst — which is exactly the resolution
    domain B6 wants: a citation into the dev's transcript must not resolve.
    Unparseable lines are skipped, not fatal: the transcript is CLI-written,
    but the resolver's job is to find citations, not to validate the file.
    """
    for path in sorted((volume_dir / "projects").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    yield block


def _call_detail(tool_input: dict) -> str:
    """One line a reader can recognize the call by. A rendering, not a record —
    the archived transcript stays the full artifact."""
    for key in ("command", "file_path", "path", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            detail = value
            break
    else:
        detail = json.dumps(tool_input, ensure_ascii=False)
    detail = " ".join(detail.split())  # newlines flatten: this lands in a table cell
    return detail if len(detail) <= 80 else detail[:77] + "..."


def resolve_citations(report: TestReport, volume_dir: Path) -> dict[str, CitedCall]:
    """Resolve every scenario's tool_use_id against this burst's transcript (B6).

    Requires, per citation: the id exists in the transcript, and its tool result
    is present and not an error. Returns ``{tool_use_id: CitedCall}`` for the
    posted table. Raises ``CitationError`` naming every citation that failed —
    the report then fails exactly like one that didn't parse. The check is
    resolvable/not-resolvable only: no grading of prose, no judging whether the
    cited call *proves* the scenario — that stays the reader's, with the record
    beside the claim.
    """
    uses: dict[str, CitedCall] = {}
    errored: set[str] = set()
    answered: set[str] = set()
    for block in _transcript_tool_blocks(volume_dir):
        if block["type"] == "tool_use" and isinstance(block.get("id"), str):
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            uses[block["id"]] = CitedCall(
                tool=str(block.get("name") or "?"), detail=_call_detail(tool_input)
            )
        elif block["type"] == "tool_result" and isinstance(block.get("tool_use_id"), str):
            answered.add(block["tool_use_id"])
            if block.get("is_error"):
                errored.add(block["tool_use_id"])

    resolved: dict[str, CitedCall] = {}
    problems: list[str] = []
    for scenario in report.scenarios:
        cited = scenario["tool_use_id"]
        if cited not in uses:
            problems.append(f"scenario {scenario['name']!r}: {cited} is not a tool call in this burst's transcript")
        elif cited not in answered:
            problems.append(f"scenario {scenario['name']!r}: {cited} has no recorded result")
        elif cited in errored:
            problems.append(f"scenario {scenario['name']!r}: {cited}'s recorded result is an error")
        else:
            resolved[cited] = uses[cited]
    if problems:
        raise CitationError("; ".join(problems))
    return resolved


def format_test_report(
    report: TestReport,
    head_sha: str,
    model: str | None,
    filed: list[int] | None = None,
    citations: dict[str, CitedCall] | None = None,
) -> str:
    """The posted report in uniform (mirrors the review overview — header,
    resolved model, the exact commit the sandbox ran). ``filed`` are the
    auto-issue numbers the pre-existing defects were relayed to. ``citations``
    is `resolve_citations`' output: the *cited call* column is filled from the
    transcript by the platform, beside the tester's own words — so a reader can
    see whether the prose matches the record (B6)."""
    meta = " · ".join(
        part for part in (f"`{model}`" if model else None, f"tested `{head_sha[:7]}`") if part
    )
    lines = ["#### 🧪 Machine test report", f"<sub>{meta}</sub>", "", report.verdict]
    if report.scenarios:
        lines += ["", "| scenario | result | cited call | evidence |", "|---|---|---|---|"]
        for s in report.scenarios:
            mark = "✅ pass" if s["result"] == "pass" else "❌ fail"
            call = (citations or {}).get(s["tool_use_id"])
            cell = f"{call.tool}: {call.detail}" if call else s["tool_use_id"]
            cell = cell.replace("`", "'").replace("|", "\\|")  # it lands in a code span in a table
            lines.append(f"| {s['name']} | {mark} | `{cell}` | {s['evidence']} |")
    if report.defects:
        lines += ["", "**Defects**", ""]
        lines += [
            f"- **{d['summary']}**"
            + (" *(pre-existing)*" if d.get("preexisting") else "")
            + (f" — {d['repro']}" if d["repro"] else "")
            for d in report.defects
        ]
    if filed:
        lines += ["", "Pre-existing defects filed as: " + ", ".join(f"#{n}" for n in filed)]
    return "\n".join(lines)
