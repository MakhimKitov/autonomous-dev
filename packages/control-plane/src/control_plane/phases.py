"""The control plane's half of the pod-side phases (G2, TO-BE §6).

When a burst runs somewhere the control plane cannot reach into — a Job pod, a
member's machine (mechanism B) — the two bytes phases run *there*, and what they
are given is the whole security story:

- **capabilities, not credentials.** A presigned GET for exactly the one snapshot
  key the ledger named, presigned PUTs for exactly the two keys this burst may
  write. No listing, no other burst's objects, and they stop working before the
  next burst starts, because they are minted at spawn and time-boxed. The pod
  holds nothing that would still be a key tomorrow.
- **the clone rides its own token.** `contents:read` on one repository, handed to
  init and to nothing else, so a reviewer or tester agent still never sees a
  GitHub credential (TO-BE §6). Minting it is `github_auth`'s; confining it to
  the init container is the runner's.
- **the ledger stays here.** The harvester ships a small record to a key; the
  control plane reads that key and folds it (`fold_artifacts`). Nothing that runs
  beside a burst writes a row, because nothing that runs beside a burst holds a
  ledger credential.

`DockerRunner` does not use any of this and should not: it shares a filesystem
with the control plane, so its phases move bytes directly (`HostPhases`, G1) and
hand the burst container nothing at all. The zero-standing-credentials property
holds on both paths for the same reason — the burst is never given a key —
by two different mechanisms, which is what `test_runner_contract.py` keeps
honest now that `JobsRunner` runs the same suite (G3).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from burst_phases.phases import (
    ARTIFACTS_PUT_URL,
    CLONE_DIR,
    CLONE_REF,
    CLONE_TOKEN,
    CLONE_URL,
    SESSION_GET_URL,
    SESSION_KEY,
    SESSION_PUT_URL,
    SESSION_SHA256,
    VOLUME_DIR,
    Artifacts,
)

from control_plane.store import ArchiveError, SessionArchive
from control_plane.volume import BurstArtifacts, Snapshot

log = structlog.get_logger()

# How long a minted capability lives. It has to outlast the slowest burst and
# die well before the next one, and the honest number is the wall-clock rail:
# `jobs.BURST_DEADLINE_S` *is* this constant, and it is what a Job carries as
# `activeDeadlineSeconds` (G3). One number, deliberately — a knob here would be a
# second way to say how long a burst may run, and the drift between the two would
# be a pod outliving its own capabilities or losing them mid-burst.
PHASE_URL_TTL_S = 3 * 60 * 60

# Where the harvester's record goes. One key per burst, beside its snapshot, so
# a burst's whole trail is one prefix and retention is one rule.
ARTIFACTS_SUFFIX = "artifacts.json"


def artifacts_key(session_key_of_burst: str) -> str:
    """The record's key, derived from *this burst's* snapshot key — never
    invented (A6), and never borrowed from another burst's.

    The argument is what `session_key(task_id, burst_id)` returns for the burst
    doing the reporting, whether or not its volume is archived: a reviewer
    archives nothing and still has to report, and two bursts resuming one parent
    must not file their records in the same place.
    """
    return session_key_of_burst.rsplit("/", 1)[0] + "/" + ARTIFACTS_SUFFIX


@dataclass(frozen=True, slots=True)
class PhaseEnv:
    """What the init and harvester containers are given, as environment.

    A plain mapping, because that is the one interface a pod spec and a
    `docker run` line share. Everything in it is either a path, a name, or a
    capability that expires; nothing in it is a credential for a store.
    """

    init: dict[str, str]
    harvest: dict[str, str]
    # The key the harvester's PUT writes to, and the one the control plane reads
    # back with `read_artifacts`. It is derived, never invented — but it is
    # derived *here*, at spawn, so nothing downstream has to re-derive it.
    record: str


def mint_phase_env(
    archive: SessionArchive,
    *,
    volume_dir: str,
    record_key: str,
    parent: Snapshot | None,
    session_key: str | None,
    clone: tuple[str, str, str, str] | None = None,
    ttl_s: float = PHASE_URL_TTL_S,
) -> PhaseEnv:
    """Mint one burst's capabilities and spell them as two environments.

    `record_key` is where the harvester reports — always this burst's own
    (`artifacts_key(session_key(task_id, burst_id))`), because every burst
    reports and only some archive. `session_key` None is a burst whose volume is
    not archived — the reviewer (B5) — and it gets no PUT for a snapshot, only
    the record's. `clone` is `(url, ref, directory, token)` for the roles whose
    repository the init container fetches; the token is the `contents:read` one
    and appears in no other container's environment.
    """
    if not record_key:
        raise ArchiveError("a burst with no record key has nowhere to report")

    init: dict[str, str] = {VOLUME_DIR: volume_dir}
    if parent is not None:
        init[SESSION_GET_URL] = archive.presign_get(parent.key, int(ttl_s))
        init[SESSION_SHA256] = parent.checksum
    if clone is not None:
        url, ref, directory, token = clone
        init[CLONE_URL] = url
        init[CLONE_REF] = ref
        init[CLONE_DIR] = directory
        init[CLONE_TOKEN] = token

    harvest: dict[str, str] = {
        VOLUME_DIR: volume_dir,
        ARTIFACTS_PUT_URL: archive.presign_put(record_key, int(ttl_s)),
    }
    if session_key is not None:
        harvest[SESSION_PUT_URL] = archive.presign_put(session_key, int(ttl_s))
        harvest[SESSION_KEY] = session_key

    log.info(
        "phase capabilities minted",
        parent=parent.key if parent else None,
        snapshot=session_key,
        record=record_key,
        ttl_s=int(ttl_s),
        clone=bool(clone),
    )
    return PhaseEnv(init=init, harvest=harvest, record=record_key)


def read_artifacts(archive: SessionArchive, record_key: str) -> BurstArtifacts:
    """What the harvester shipped, as the control plane's own shape.

    The one place the two halves meet: a `BurstArtifacts` from a pod is
    indistinguishable from one a host phase produced, so `fold_artifacts` — and
    every rail behind it — cannot tell where the burst ran.
    """
    blob = archive.read_object(record_key)
    if blob is None:
        raise ArchiveError(f"the harvester left no record at {record_key}")
    shipped = Artifacts.from_json(blob)
    return BurstArtifacts(
        result=shipped.result,
        violations=list(shipped.violations),
        session_key=shipped.session_key,
        session_checksum=shipped.session_sha256,
    )
