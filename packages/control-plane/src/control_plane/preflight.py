"""Registration preflight (TO-BE §2, C2): the operability gate + socket lint.

Two verdicts with a sharp boundary. The **gate** refuses declarations the
platform cannot execute: the org App can't read the repo; a review provider
declared without its credentials (yesterday's scheduler-startup error,
relocated to registration); `e2e on` without a parseable ``TESTING.md`` whose
declared env the product's secret refs cover and resolve. Everything else —
``CLAUDE.md``, issue forms, the PR template, a commitlint config — is **socket
lint**: checked, warned, recorded in the registration's audit row, never a
refusal. A socket-poor repo registers and works conservatively; the socket
improves while the platform runs, and ``product check`` re-runs this same
function to catch drift.

The probe reads the repo through the org App's installation token, so "the
App can read the repo" and "the manifest is fetchable" are one fact, not two
checks that can disagree.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from control_plane.github_api import GitHubApiError
from control_plane.github_auth import GitHubAuthError
from control_plane.models import RepositoryRecord
from control_plane.review import ReviewPolicy, ReviewPolicyError
from control_plane.testing import (
    MANIFEST_FILENAME,
    E2EPolicy,
    E2EPolicyError,
    ManifestError,
    TestManifest,
    parse_manifest,
)

# the commit vocabulary is shared platform-wide (GITHUB-INTERFACE.md); any of
# these at the repo root counts as the product enforcing it
COMMITLINT_CONFIGS = (
    ".commitlintrc.json",
    ".commitlintrc.yml",
    ".commitlintrc",
    "commitlint.config.js",
    "commitlint.config.mjs",
)


class PreflightError(RuntimeError):
    """The declared product cannot be operated — refuse before it becomes a row."""


class ProductRepo(Protocol):
    """What preflight needs to see of the product repo.

    ``GitHubApi`` provides it (reading as the org App is itself the access
    gate); tests fake it with a dict of files.
    """

    def repo_owner(self) -> str: ...
    def read_file(self, path: str) -> str | None: ...
    def path_exists(self, path: str) -> bool: ...


def run_preflight(
    row: RepositoryRecord, repo: ProductRepo, environ: dict[str, str] | None = None
) -> list[str]:
    """Gate + lint the declaration ``row`` against the live repo.

    Raises ``PreflightError`` when the platform could not execute the
    declaration; returns the socket-lint warnings otherwise. The caller decides
    where the warnings land — registration records them in its audit row,
    ``product check`` prints them.
    """
    env = dict(os.environ) if environ is None else environ

    # 1. The org App reads the repo — every check below rides this access.
    try:
        repo.repo_owner()
    except (GitHubApiError, GitHubAuthError) as exc:
        raise PreflightError(
            f"the App cannot read {row.repository} — is it installed on the repo? "
            f"`control-plane github-check` shows the installation ({exc})"
        ) from exc

    # 2. Declared review providers must have their credentials — the startup
    #    question, asked where the declaration is made. The same constructor
    #    the scheduler uses on a registered row (C3), so registration and
    #    startup can never disagree about honorability.
    try:
        ReviewPolicy.from_product(row, env)
    except ReviewPolicyError as exc:
        raise PreflightError(str(exc)) from exc

    # 3. `e2e on` must be executable end to end: the tester App exists (its
    #    identity posts the reports — D3), the funnel has a trigger, the
    #    manifest parses, and every env name it declares maps to a ref that
    #    points at something. Values are never read here — D4 resolves them.
    manifest, problem = _manifest_state(repo)
    if row.e2e_enabled:
        try:
            E2EPolicy.from_product(row, env)
        except E2EPolicyError as exc:
            raise PreflightError(str(exc)) from exc
        providers = row.review_provider.split(",") if row.review_provider else []
        if not (row.review_enabled and "claude" in providers):
            raise PreflightError(
                "e2e on requires the claude review provider — the test stage "
                "triggers on its APPROVE (TESTER.md)"
            )
        if manifest is None:
            raise PreflightError(f"e2e on, but {row.repository} {problem}")
        uncovered = [name for name in manifest.env if name not in row.test_env_refs]
        if uncovered:
            raise PreflightError(
                f"e2e on, but {MANIFEST_FILENAME} declares env the product has no "
                f"ref for: {', '.join(uncovered)} — declare with --test-env NAME=env:REF"
            )
        unresolved = [
            f"{name}={ref}"
            for name, ref in sorted(row.test_env_refs.items())
            if name in manifest.env and not _ref_resolves(ref, env)
        ]
        if unresolved:
            raise PreflightError(
                "e2e on, but these sandbox refs resolve to nothing on this machine: "
                + ", ".join(unresolved)
            )

    # 4. Socket lint — everything the repo could offer but doesn't. Warnings,
    #    never refusals: the boundary is executability, and the platform works
    #    a socket-poor repo conservatively.
    warnings: list[str] = []
    if manifest is None:
        if not row.e2e_enabled:  # with e2e on the gate already spoke
            warnings.append(
                f"{problem} — not e2e-testable; `--e2e on` will refuse until a "
                "parseable one ships"
            )
    else:
        stale = sorted(set(row.test_env_refs) - set(manifest.env))
        if stale:
            warnings.append(
                f"test-env refs {MANIFEST_FILENAME} never asks for: {', '.join(stale)} "
                "— a stale declaration, or the manifest drifted"
            )
    if repo.read_file("CLAUDE.md") is None:
        warnings.append("no CLAUDE.md — the dev agent works without the product's operating contract")
    if not repo.path_exists(".github/ISSUE_TEMPLATE"):
        warnings.append("no issue forms (.github/ISSUE_TEMPLATE/) — intake arrives unstructured")
    if not repo.path_exists(".github/PULL_REQUEST_TEMPLATE.md"):
        warnings.append("no PR template — deliveries carry no uniform shape")
    if not any(repo.path_exists(path) for path in COMMITLINT_CONFIGS):
        warnings.append(
            "no commitlint config — the shared commit vocabulary is unenforced (GITHUB-INTERFACE.md)"
        )
    return warnings


def _manifest_state(repo: ProductRepo) -> tuple[TestManifest | None, str]:
    """The parsed TESTING.md, or None plus what's wrong with it — one read
    serving both the gate (e2e on) and the lint (e2e off)."""
    text = repo.read_file(MANIFEST_FILENAME)
    if text is None:
        return None, f"ships no {MANIFEST_FILENAME}"
    try:
        return parse_manifest(text), ""
    except ManifestError as exc:
        return None, f"has an unusable {MANIFEST_FILENAME}: {exc}"


def _ref_resolves(ref: str, env: dict[str, str]) -> bool:
    """Whether a declared secret ref points at something, without reading it —
    preflight proves executability; values stay where they live (D4 resolves)."""
    scheme, _, name = ref.partition(":")
    if scheme == "env":
        return bool(env.get(name, "").strip())
    return Path(name).expanduser().is_file()
