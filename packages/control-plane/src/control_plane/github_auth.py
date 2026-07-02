"""GitHub App auth: mint short-lived installation tokens for bursts.

The App's private key never leaves the control plane (a wall, §8). Each burst
gets a fresh **installation access token** — 1-hour expiry, scoped to whatever
the App was granted (contents + pull_requests + issues write; no merge/admin).
Token lifetime bounding burst lifetime is a feature: keep the wall-clock cap
under an hour or the push at the end of a long burst fails.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import jwt
import requests

GITHUB_API = "https://api.github.com"

APP_ID = "GITHUB_APP_ID"
PRIVATE_KEY_PATH = "GITHUB_APP_PRIVATE_KEY_PATH"
INSTALLATION_ID = "GITHUB_APP_INSTALLATION_ID"

# requests-compatible callables, injectable for tests
HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]


class GitHubAuthError(RuntimeError):
    """App credentials are missing, wrong, or the exchange failed."""


@dataclass(slots=True)
class AppCredentials:
    app_id: str
    private_key: str
    installation_id: str | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> AppCredentials | None:
        """None when the App is not configured (burst runs without GitHub)."""
        env = dict(os.environ) if environ is None else environ
        app_id = env.get(APP_ID, "").strip()
        key_path = env.get(PRIVATE_KEY_PATH, "").strip()
        if not app_id and not key_path:
            return None
        if not app_id or not key_path:
            raise GitHubAuthError(f"set both {APP_ID} and {PRIVATE_KEY_PATH} (or neither)")
        path = Path(key_path).expanduser()
        if not path.is_file():
            raise GitHubAuthError(f"{PRIVATE_KEY_PATH} does not exist: {path}")
        return cls(
            app_id=app_id,
            private_key=path.read_text(encoding="utf-8"),
            installation_id=env.get(INSTALLATION_ID, "").strip() or None,
        )


@dataclass(slots=True)
class InstallationToken:
    token: str
    expires_at: str
    permissions: dict[str, str]
    app_slug: str = ""

    @property
    def bot_login(self) -> str:
        return f"{self.app_slug}[bot]"


def make_app_jwt(creds: AppCredentials, now: int | None = None) -> str:
    """A 9-minute RS256 JWT identifying the App itself (not an installation)."""
    at = int(time.time()) if now is None else now
    payload = {"iat": at - 60, "exp": at + 540, "iss": creds.app_id}
    return jwt.encode(payload, creds.private_key, algorithm="RS256")


def _check(response: Any, what: str) -> Any:
    if response.status_code >= 300:
        raise GitHubAuthError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")
    return response.json()


def discover_installation(
    creds: AppCredentials, http_get: HttpGet = requests.get
) -> tuple[str, str]:
    """(installation_id, app_slug). Requires exactly one installation unless pinned."""
    headers = {"Authorization": f"Bearer {make_app_jwt(creds)}"}
    installations = _check(
        http_get(f"{GITHUB_API}/app/installations", headers=headers, timeout=30),
        "listing installations",
    )
    if not installations:
        raise GitHubAuthError("the App has no installations — install it on the product repo")
    if creds.installation_id:
        for inst in installations:
            if str(inst["id"]) == creds.installation_id:
                return creds.installation_id, inst["app_slug"]
        raise GitHubAuthError(f"installation {creds.installation_id} not found for this App")
    if len(installations) > 1:
        ids = ", ".join(str(inst["id"]) for inst in installations)
        raise GitHubAuthError(f"multiple installations ({ids}) — set {INSTALLATION_ID}")
    return str(installations[0]["id"]), installations[0]["app_slug"]


def mint_installation_token(
    creds: AppCredentials,
    http_get: HttpGet = requests.get,
    http_post: HttpPost = requests.post,
) -> InstallationToken:
    installation_id, app_slug = discover_installation(creds, http_get)
    headers = {"Authorization": f"Bearer {make_app_jwt(creds)}"}
    data = _check(
        http_post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers=headers,
            timeout=30,
        ),
        "minting installation token",
    )
    return InstallationToken(
        token=data["token"],
        expires_at=data.get("expires_at", ""),
        permissions=data.get("permissions", {}),
        app_slug=app_slug,
    )


def bot_git_identity(token: InstallationToken, app_id: str) -> tuple[str, str]:
    """(user.name, user.email) for commits authored by the App bot."""
    name = token.bot_login
    email = f"{app_id}+{name}@users.noreply.github.com"
    return name, email
