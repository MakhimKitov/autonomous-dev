"""The scheduler's GitHub surface: read facts, reflect state (GITHUB-INTERFACE.md).

One repo, App-installation auth, a requests-compatible injectable transport —
the same testing seam as ``github_auth``. Every call is a small explicit REST
operation; pagination is a single page (phase-0 scale: a handful of open
issues and PRs).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from control_plane.github_auth import GITHUB_API

COPILOT_REVIEWER = "copilot-pull-request-reviewer[bot]"

HttpRequest = Callable[..., Any]


class GitHubApiError(RuntimeError):
    """A GitHub call the scheduler depends on failed."""


@dataclass(slots=True)
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]

    @property
    def provenance(self) -> str:
        """``human`` only when explicitly labelled; everything else is tainted."""
        return "human" if "human" in self.labels else "auto"


@dataclass(slots=True)
class PullRequest:
    number: int
    state: str  # open | closed (merged PRs are closed + merged)
    merged: bool
    head_ref: str
    head_sha: str


@dataclass(slots=True)
class Review:
    id: int
    author: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    body: str


@dataclass(slots=True)
class ReviewComment:
    id: int  # the thread id the dev replies to (pulls/<n>/comments/<id>/replies)
    review_id: int | None
    author: str
    path: str
    line: int | None
    body: str


def _issue_of(item: dict) -> Issue:
    labels = [l["name"] if isinstance(l, dict) else str(l) for l in item.get("labels", [])]
    return Issue(
        number=item["number"],
        title=item.get("title", ""),
        body=item.get("body") or "",
        labels=labels,
    )


def _pr_of(item: dict) -> PullRequest:
    head = item.get("head") or {}
    return PullRequest(
        number=item["number"],
        state=item.get("state", ""),
        merged=bool(item.get("merged") or item.get("merged_at")),
        head_ref=head.get("ref", ""),
        head_sha=head.get("sha", ""),
    )


class GitHubApi:
    """Operations against one repo. ``token`` may be a string or a supplier
    callable (the scheduler passes ``InstallationTokenCache`` so hour-long
    runs survive token expiry)."""

    def __init__(
        self,
        repo: str,
        token: str | Callable[[], str],
        request: HttpRequest = requests.request,
    ) -> None:
        self.repo = repo  # "owner/name"
        self._token = token if callable(token) else (lambda: token)
        self._request = request

    def _call(
        self,
        method: str,
        path: str,
        what: str,
        *,
        params: dict | None = None,
        json: Any = None,
        ok: tuple[int, ...] = (),
    ) -> Any:
        response = self._request(
            method,
            f"{GITHUB_API}/repos/{self.repo}{path}",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/vnd.github+json",
            },
            params=params,
            json=json,
            timeout=30,
        )
        if response.status_code >= 300 and response.status_code not in ok:
            raise GitHubApiError(f"{what}: HTTP {response.status_code} {response.text[:300]}")
        try:
            return response.json()
        except ValueError:
            return None

    # --- issues ---

    def list_ready_issues(self) -> list[Issue]:
        data = self._call(
            "GET",
            "/issues",
            "listing ready issues",
            params={"labels": "ready", "state": "open", "per_page": 100},
        )
        # the issues endpoint returns PRs too — they carry a pull_request key
        return [_issue_of(item) for item in data if "pull_request" not in item]

    def get_issue(self, number: int) -> Issue:
        return _issue_of(self._call("GET", f"/issues/{number}", f"reading issue #{number}"))

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._call("POST", f"/issues/{number}/labels", f"labelling #{number}", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        # 404 = the label wasn't there — the state we wanted
        self._call("DELETE", f"/issues/{number}/labels/{label}", f"unlabelling #{number}", ok=(404,))

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", f"commenting on #{number}", json={"body": body})

    # --- pull requests ---

    def open_pr_with_branch_prefix(self, prefix: str) -> PullRequest | None:
        """The dev's PR for an issue: head branch ``dev/<n>-<slug>``."""
        pulls = self._call("GET", "/pulls", "listing open PRs", params={"state": "open", "per_page": 100})
        for item in pulls:
            if (item.get("head") or {}).get("ref", "").startswith(prefix):
                return _pr_of(item)
        return None

    def get_pr(self, number: int) -> PullRequest:
        return _pr_of(self._call("GET", f"/pulls/{number}", f"reading PR #{number}"))

    def list_reviews(self, pr_number: int) -> list[Review]:
        data = self._call(
            "GET", f"/pulls/{pr_number}/reviews", f"listing reviews of PR #{pr_number}", params={"per_page": 100}
        )
        return [
            Review(
                id=item["id"],
                author=(item.get("user") or {}).get("login", ""),
                state=item.get("state", ""),
                body=item.get("body") or "",
            )
            for item in data
        ]

    def list_review_comments(self, pr_number: int) -> list[ReviewComment]:
        data = self._call(
            "GET", f"/pulls/{pr_number}/comments", f"listing review comments of PR #{pr_number}", params={"per_page": 100}
        )
        return [
            ReviewComment(
                id=item["id"],
                review_id=item.get("pull_request_review_id"),
                author=(item.get("user") or {}).get("login", ""),
                path=item.get("path", ""),
                line=item.get("line"),
                body=item.get("body") or "",
            )
            for item in data
        ]

    def request_reviewers(self, pr_number: int, reviewers: list[str]) -> None:
        self._call(
            "POST",
            f"/pulls/{pr_number}/requested_reviewers",
            f"re-requesting review on PR #{pr_number}",
            json={"reviewers": reviewers},
        )

    def repo_owner(self) -> str:
        return self._call("GET", "", "reading the repo")["owner"]["login"]


def slug_from_origin(repo: Path) -> str | None:
    """``owner/name`` parsed from the clone's origin URL (ssh or https)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", out.stdout.strip())
    return match.group(1) if match else None
