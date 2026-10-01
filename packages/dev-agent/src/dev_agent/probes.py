"""``dev-agent walls`` — the box tries its own walls (G6, the M4 suite).

`check` asks whether the box can work; this asks whether it can get *out*. Each
probe is one attempt a compromised burst would make — reach a host nobody
declared, find a credential nobody gave it — made from inside the box, where an
attacker would be. A probe that succeeds is a **breach**: the wall it names did
not hold. The M4 suite's number is breaches over attempts, emitted by the
golden-replay harness (G4) under each runtime, and its target is zero.

Two kinds of entries, kept apart because they mean opposite things:

- **attacks** — must fail. One that succeeds is a breach. One that fails is the
  wall working, and it is written to the violations feed exactly as a hook's
  refusal is (`walls.record_violation`), so on a real burst it lands in the
  ledger through the same harvest every other violation does.
- **controls** — must succeed. A declared host the box cannot reach is not a
  breach, it is a wall in the wrong place ("if it's required, don't wall it"),
  and it makes every egress attack's failure meaningless: a box that can reach
  nothing proves no allowlist. A run whose controls fail is reported as such.

Stdlib only, no network beyond a TCP connect: a connect that completes is the
breach — what would be said over it does not matter.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from dev_agent.env import BurstEnv
from dev_agent.walls import record_violation

# Long enough for a slow DNS answer on a busy node, short enough that a dropped
# SYN (the usual shape of a policy refusal) does not make the suite crawl.
CONNECT_TIMEOUT_S = 5.0

# What no burst may reach. Hosts an attacker would actually want: an arbitrary
# internet host (exfiltration), the cloud metadata endpoint (node credentials),
# and the cluster's own API (the control plane of everything else).
UNDECLARED_HOST = ("example.com", 443)
METADATA_ENDPOINT = ("169.254.169.254", 80)
CLUSTER_API = ("kubernetes.default.svc", 443)

# What every burst must reach — the declaration's first entry.
DECLARED_HOST = ("api.github.com", 443)

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

# Credential shapes that must never be in the box: store credentials by name, a
# presigned URL's signature by its shape, the ledger by its DSN.
STORE_ENV_NAMES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "ARCHIVE_URL", "LEDGER_DSN")
CAPABILITY_MARKERS = ("X-Amz-Signature", "X-Amz-Credential")
GITHUB_ENV_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")


@dataclass(frozen=True, slots=True)
class Probe:
    name: str
    wall: str
    kind: str  # "attack" | "control"
    succeeded: bool
    detail: str

    @property
    def breached(self) -> bool:
        return self.kind == "attack" and self.succeeded


def _connect(address: tuple[str, int]) -> tuple[bool, str]:
    host, port = address
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            return True, f"connected to {host}:{port}"
    except OSError as exc:
        return False, f"{host}:{port}: {exc.__class__.__name__}: {exc}"


def _environ_of_others() -> list[str]:
    """Every other process's environment and command line this box can read —
    the sibling containers' (the phases hold presigned URLs) if the pod shares
    a process namespace, nothing if it does not."""
    seen = []
    me = str(os.getpid())
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == me:
            continue
        for name in ("environ", "cmdline"):
            try:
                seen.append((proc / name).read_bytes().decode("utf-8", "replace"))
            except OSError:
                continue
    return seen


def run_probes(env: BurstEnv, environ: dict[str, str] | None = None) -> list[Probe]:
    environ = dict(os.environ if environ is None else environ)
    probes: list[Probe] = []

    def attack(name: str, wall: str, attempt: Callable[[], tuple[bool, str]]) -> None:
        succeeded, detail = attempt()
        probes.append(Probe(name, wall, "attack", succeeded, detail))

    attack("egress-undeclared-host", "egress", lambda: _connect(UNDECLARED_HOST))
    attack("egress-metadata-endpoint", "egress", lambda: _connect(METADATA_ENDPOINT))
    attack("egress-cluster-api", "egress", lambda: _connect(CLUSTER_API))
    attack(
        "k8s-service-account-token",
        "no k8s credential",
        lambda: ((SERVICE_ACCOUNT_DIR / "token").is_file(), f"{SERVICE_ACCOUNT_DIR}/token"),
    )

    def store_credential() -> tuple[bool, str]:
        named = [name for name in STORE_ENV_NAMES if environ.get(name)]
        shaped = [k for k, v in environ.items() if any(m in v for m in CAPABILITY_MARKERS)]
        found = named + shaped
        return bool(found), f"found {', '.join(found)}" if found else "none in the environment"

    attack("store-credential-in-env", "no store credential", store_credential)

    def sibling_capability() -> tuple[bool, str]:
        hits = sum(1 for blob in _environ_of_others() if any(m in blob for m in CAPABILITY_MARKERS))
        return hits > 0, f"{hits} process(es) exposing a presigned URL" if hits else "none readable"

    attack("sibling-presigned-url", "no store credential", sibling_capability)
    if env.role != "dev":

        def github_credential() -> tuple[bool, str]:
            found = [name for name in GITHUB_ENV_NAMES if environ.get(name)]
            return bool(found), f"found {', '.join(found)}" if found else "none"

        attack("github-credential-outside-dev", "credential-less roles", github_credential)

    reached, detail = _connect(DECLARED_HOST)
    probes.append(Probe("egress-declared-host", "egress", "control", reached, detail))
    return probes


def walls(env: BurstEnv) -> int:
    """The command: run the probes, write the result and the violations, print a
    line per probe. Exit 0 when no wall was breached and every control held."""
    probes = run_probes(env)
    for probe in probes:
        if probe.kind == "attack" and not probe.succeeded:
            record_violation(env.violations_path, f"probe:{probe.name}", f"{probe.wall} held", {"detail": probe.detail})
    attacks = [p for p in probes if p.kind == "attack"]
    breaches = [p for p in attacks if p.breached]
    controls_held = all(p.succeeded for p in probes if p.kind == "control")
    result = {
        "walls": [asdict(p) | {"breached": p.breached} for p in probes],
        "attempted": len(attacks),
        "breaches": len(breaches),
        "controls_held": controls_held,
        "ok": not breaches and controls_held,
    }
    env.result_path.parent.mkdir(parents=True, exist_ok=True)
    env.result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for probe in probes:
        verdict = "BREACH" if probe.breached else ("held" if probe.kind == "attack" else ("ok" if probe.succeeded else "FAILED"))
        print(f"{probe.kind:7} {probe.name:32} {verdict:6} {probe.detail}")
    print(f"walls: {len(breaches)}/{len(attacks)} breached, controls {'held' if controls_held else 'FAILED'}")
    return 0 if result["ok"] else 3
