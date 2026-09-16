"""The burst volume's shape — what is in it, and how it is packed (TO-BE §6).

One control-plane-owned directory is the whole interface between a burst and the
platform: the parent snapshot goes in before boot, the agent writes its result
and its violations feed into it, and afterwards the directory is packed into one
immutable object (A5, "the session tree"). Three parties need to agree on that
shape and none of them may import the others:

- the **dev agent**, which writes the two files from inside the box;
- the **control plane**, which materializes and folds;
- the **pod-side phases** (G2): an init container that unpacks the parent and a
  harvester that packs the result, neither of which may import control-plane
  code — nothing that runs beside a burst holds ledger or storage credentials,
  and the surest way to keep that true is to keep the code out of reach.

So the shape lives here, stdlib only, the way `secret-refs` holds the one
grammar its two dependents share. Nothing in this module does I/O to a store: it
packs, unpacks, and names.

**The credential exclusion is unconditional.** `EXCLUDE_FROM_ARCHIVE` is applied
by the packer itself, not by its callers, so a snapshot cannot carry a token
whoever packed it — the control plane, or a harvester in a pod.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

# What the agent leaves in the volume. The names are the contract between the
# worker that writes them and everything that reads them back.
BURST_RESULT_FILENAME = "burst-result.json"
VIOLATIONS_FILENAME = "violations.jsonl"

# Never archived, whoever packs. The CLI writes its OAuth material into the
# session directory; a snapshot is evidence and gets read by people.
EXCLUDE_FROM_ARCHIVE = {".credentials.json", "auth.json"}


def tar_without_credentials(session_dir: Path, destination: Path) -> str:
    """Tar session_dir to destination, minus credential files. Returns sha256."""

    def _skip_credentials(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if Path(info.name).name in EXCLUDE_FROM_ARCHIVE else info

    with tarfile.open(destination, "w:gz") as tar:
        tar.add(session_dir, arcname=".", filter=_skip_credentials)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def untar(blob: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(blob, "r:gz") as tar:
        tar.extractall(dest_dir, filter="data")


def sha256_of(path: Path) -> str:
    """The checksum of an object on disk — the number the ledger is authority for."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
