"""Platform-owned product clones (issue #4).

Bursts must never mount the operator's working copy: a burst mutates its
checkout — branch left checked out, ``--local`` git config rewritten to the
bot identity, bytecode churn — and all of it leaked into the operator's
environment. The control plane owns one clone per repo under
``<data>/clones/`` and re-syncs it to origin's default branch before every
burst; whatever a burst leaves behind is platform state, not the operator's.

Always HTTPS (the container has no ssh, by design). For private repos the
token travels as a per-invocation header — it is never written into the
clone's config, which gets mounted into the box.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path


class CloneError(RuntimeError):
    """The platform clone could not be created or synced."""


def _auth_config(token: str | None) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


def _git(args: list[str], cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        verb = next((a for a in args if not a.startswith("-") and "extraheader" not in a), "?")
        # never echo args — the auth header may be among them
        raise CloneError(f"git {verb} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip() if capture else ""


def prepare_clone(
    clones_root: Path,
    slug: str,
    token: str | None = None,
    url: str | None = None,
    ref: str | None = None,
) -> Path:
    """Ensure the platform's clone of ``slug`` exists and exactly matches
    origin's default branch — or ``ref`` (a sha: reviewer bursts check out the
    PR head, detached). Returns its path. ``url`` overrides the origin (tests
    use file:// origins)."""
    clone = clones_root / slug.replace("/", "__")
    origin = url or f"https://github.com/{slug}.git"
    auth = _auth_config(token) if url is None else []
    if not (clone / ".git").is_dir():
        clones_root.mkdir(parents=True, exist_ok=True)
        _git([*auth, "clone", "--quiet", origin, str(clone)])
    _git([*auth, "fetch", "--quiet", "--prune", "origin"], cwd=clone)
    _git([*auth, "remote", "set-head", "origin", "--auto"], cwd=clone)
    _git(["reset", "--hard", "--quiet"], cwd=clone)  # clean tree so checkout can't conflict
    if ref:
        _git(["checkout", "--quiet", "--detach", ref], cwd=clone)
    else:
        default_ref = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=clone, capture=True)
        branch = default_ref.split("/", 1)[1]
        _git(["checkout", "--quiet", "-B", branch, default_ref], cwd=clone)
    _git(["clean", "-fdq"], cwd=clone)  # ignored files survive (warm .venv)
    return clone
