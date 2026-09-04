"""SecretResolver — secrets materialized by the adopter, resolved by ref (TO-BE §4, D4).

The platform never speaks to a secret manager. Everywhere it needs a secret it
holds a **ref** — a name, never a value, never a command — and resolves it at
the moment of use: an App key when a token is minted, a sandbox credential when
a tester burst spawns. Three schemes, deliberately few:

- ``env:NAME``   — the value of an environment variable of the control plane;
- ``file:PATH``  — the contents of a file the adopter's tooling wrote;
- ``helper:NAME`` — the stdout of one helper binary configured out-of-band
  (``SECRET_HELPER``, the git-credential-helper pattern), invoked as
  ``<helper> get NAME``. Anything a vault or an agent can do, the helper does,
  and the platform stays ignorant of which one.

How secrets reach env, files or the helper is the adopter's instrument
(capabilities, not instruments — §12). Refs are what the ledger stores
(``repositories.test_env_refs``, later ``subscriptions.secret_ref``); values
exist in memory between resolve and inject, and in the container env until it
dies. Nothing here logs a value.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SECRET_HELPER_ENV = "SECRET_HELPER"  # path to the helper binary — the one out-of-band knob
SCHEMES: tuple[str, ...] = ("env", "file", "helper")
HELPER_TIMEOUT_S = 30  # a helper may talk to a vault; a hang must not stall the tick


class SecretError(RuntimeError):
    """The ref is malformed, or it points at nothing here. Never carries a value."""


def parse_ref(ref: str, *, bare_as: str | None = None) -> tuple[str, str]:
    """Split ``scheme:name``; refuse unknown schemes so a value can never pass
    for a ref. ``bare_as`` gives a scheme-less string a legacy meaning — the
    ``*_GITHUB_APP_PRIVATE_KEY_PATH`` variables read a plain path as ``file:``."""
    ref = (ref or "").strip()
    scheme, sep, name = ref.partition(":")
    if sep and scheme in SCHEMES:
        if not name.strip():
            raise SecretError(f"secret ref {ref!r} names nothing after {scheme}:")
        return scheme, name.strip()
    if bare_as is not None and ref and not sep:
        return bare_as, ref
    raise SecretError(
        f"a secret ref is env:NAME, file:PATH or helper:NAME — a reference, never a value "
        f"(got {ref!r})"
    )


@dataclass(frozen=True, slots=True)
class SecretResolver:
    """Resolves refs against this machine. ``environ`` None reads the live
    process env at each call (the scheduler's case); a dict pins it (tests,
    preflight probes)."""

    environ: Mapping[str, str] | None = None

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self.environ is None else self.environ

    @property
    def helper(self) -> str | None:
        """The configured helper binary, or None — the ``helper:`` scheme is
        unavailable until the adopter points ``SECRET_HELPER`` at one."""
        return (self.env.get(SECRET_HELPER_ENV) or "").strip() or None

    def resolve(self, ref: str, *, bare_as: str | None = None) -> str:
        """The secret behind ``ref``, trailing newlines removed — a file or a
        helper's stdout ends with one by convention, a credential never does."""
        scheme, name = parse_ref(ref, bare_as=bare_as)
        if scheme == "env":
            value = self.env.get(name, "")
            if not value.strip():
                raise SecretError(f"env var {name} is unset")
            return value.rstrip("\r\n")
        if scheme == "file":
            path = Path(name).expanduser()
            if not path.is_file():
                raise SecretError(f"file {path} does not exist")
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        return self._ask_helper(name)

    def probe(self, ref: str, *, bare_as: str | None = None) -> bool:
        """Whether ``ref`` points at something, without reading it — the
        preflight question (values stay where they live). A ``helper:`` ref
        probes as "the helper is configured and executable": invoking it would
        read the value, and a helper may audit or prompt on every call."""
        try:
            scheme, name = parse_ref(ref, bare_as=bare_as)
        except SecretError:
            return False
        if scheme == "env":
            return bool(self.env.get(name, "").strip())
        if scheme == "file":
            return Path(name).expanduser().is_file()
        return self._helper_path() is not None

    def _helper_path(self) -> str | None:
        helper = self.helper
        if helper is None:
            return None
        return shutil.which(helper) or (helper if os.access(helper, os.X_OK) else None)

    def _ask_helper(self, name: str) -> str:
        if self.helper is None:
            raise SecretError(f"helper:{name} — no helper configured; set {SECRET_HELPER_ENV}")
        binary = self._helper_path()
        if binary is None:
            raise SecretError(f"{SECRET_HELPER_ENV}={self.helper} is not an executable")
        try:
            proc = subprocess.run(
                [binary, "get", name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=HELPER_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SecretError(f"helper did not answer for {name} within {HELPER_TIMEOUT_S}s") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[-1:] or ["no stderr"]
            raise SecretError(f"helper failed for {name} (exit {proc.returncode}): {detail[0]}")
        value = proc.stdout.rstrip("\r\n")
        if not value.strip():
            raise SecretError(f"helper returned nothing for {name}")
        return value
