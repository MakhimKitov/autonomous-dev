"""The two entrypoints, as a pod (or `docker run`) invokes them.

No arguments: everything is environment, because that is the one interface a pod
spec and a `docker run` line both have. Exit codes are the whole protocol — a
non-zero init fails the pod before the agent boots, and a non-zero harvester
tells the control plane that what it is about to read may not be there.
"""

from __future__ import annotations

import os
import sys

from burst_phases.phases import harvest, initialize
from burst_phases.phases import _run as run_phase


def init_main(argv: list[str] | None = None) -> int:  # noqa: ARG001 — the shape every entrypoint has
    return run_phase(initialize, dict(os.environ), "burst-init")


def harvest_main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    return run_phase(harvest, dict(os.environ), "burst-harvest")


if __name__ == "__main__":  # pragma: no cover — the image's entrypoint
    sys.exit(init_main() if sys.argv[1:2] == ["init"] else harvest_main())
