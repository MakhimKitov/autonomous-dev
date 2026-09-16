"""The cluster rig: what `JobsRunner` is tested against, and how to get one.

A Job runner cannot be faked the way `DockerRunner` can (a shell in the daemon's
seat): what is being tested *is* the pod — three containers sharing a volume, a
service-account token that is not mounted, capabilities that expire. So this rig
needs a real cluster, and the tests that use it skip without one.

One command builds it (`scripts/burst-cluster.sh`), and it is a throwaway kind
cluster. The two things it has to arrange are worth stating, because they are
what makes a *local* cluster able to run this at all:

- **the archive must answer to one address from both sides.** A presigned URL is
  signed over its Host header, so the control plane on the host and the pod in
  the cluster have to reach the object store by the same string. On a laptop
  that string is the host's own LAN address: published ports answer there, and a
  pod's egress reaches it through the node.
- **the images must be in the cluster**, since there is no registry: the phases
  image is loaded into the node, and the contract's bursts run *that* image too
  — their "agent" is a shell script, so any image with a shell would do, and
  using the one already loaded keeps the rig to a single artifact.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from control_plane.jobs import DEFAULT_PHASES_IMAGE, JobsRunner, job_name
from control_plane.objectstore import ObjectSessionArchive
from control_plane.runner import NAMESPACE_ENV, BurstSpec
from control_plane.store import ARCHIVE_URL_ENV

PHASES_IMAGE_ENV = "BURST_PHASES_IMAGE"

# Read at import, like every other file here that means to reach a service
# backend: `tests/conftest.py` deletes ARCHIVE_URL for the whole suite so that a
# test wanting scratch space cannot accidentally get the shared bucket.
NAMESPACE = os.environ.get(NAMESPACE_ENV, "").strip()
ARCHIVE = os.environ.get(ARCHIVE_URL_ENV, "").strip()
PHASES_IMAGE = os.environ.get(PHASES_IMAGE_ENV, "").strip() or DEFAULT_PHASES_IMAGE

# Long enough for an image pull and a snapshot, short enough that a wedged pod
# in a test cluster does not sit there for the production three hours.
RIG_DEADLINE_S = 300


def cluster_or_skip() -> tuple[str, str, str]:
    """``(namespace, archive url, phases image)`` — or a skip that says what is
    missing. Nothing here creates anything: a test suite that stands up a
    cluster hides how long it takes and what it needs."""
    if not NAMESPACE or not ARCHIVE:
        pytest.skip(f"no burst cluster: set {NAMESPACE_ENV} and {ARCHIVE_URL_ENV} (scripts/burst-cluster.sh)")
    if shutil.which("kubectl") is None:
        pytest.skip("no kubectl on PATH")
    probe = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "serviceaccount", "default"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"namespace {NAMESPACE!r} is not reachable — is the cluster up?")
    return NAMESPACE, ARCHIVE, PHASES_IMAGE


class ScriptedJobs(JobsRunner):
    """`JobsRunner` whose agent container runs a shell script instead of the
    dev-agent image's entrypoint — the one thing a contract test needs to vary,
    and the same seam `ShellDocker` opens on the docker side."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.scripts: dict[str, str] = {}

    def agent_script(self, spec: BurstSpec) -> str:
        return self.scripts[spec.burst_id]


class JobsRig:
    """Bursts as Jobs on a real cluster."""

    def __init__(self, tmp_path: Path):
        namespace, url, image = cluster_or_skip()
        self.archive = ObjectSessionArchive.from_url(url)
        self.runner = ScriptedJobs(
            self.archive, namespace=namespace, phases_image=image, deadline_s=RIG_DEADLINE_S
        )
        # every burst in a namespace needs its own name, and tests reuse `b1`
        self._run = uuid.uuid4().hex[:6]
        self._spawned: list[str] = []
        self.volumes = tmp_path / "volumes"  # unused on a cluster; the spec still carries one

    def burst(self, script: str, burst_id: str = "b1") -> BurstSpec:
        unique = f"{burst_id}-{self._run}"
        self.runner.scripts[unique] = script
        self._spawned.append(unique)
        return BurstSpec(
            task_id="owner/product#42",
            burst_id=unique,
            volume_dir=self.volumes / unique,
            repo_mount=Path("/clones/product"),
            image=self.runner.phases_image,
        )

    def key(self, name: str) -> str:
        """A key this run alone claims: snapshots are write-once, and the bucket
        behind a cluster outlives the test that wrote into it."""
        return f"sessions/owner/product#42/{name}-{self._run}/session.tar.gz"

    def packaged_env(self, spec: BurstSpec) -> dict[str, str]:
        """The env the pod's agent container is given, read back off the manifest."""
        self.runner.materialize(spec, None)
        pending = self.runner._pending[spec.burst_id]
        manifest = self.runner.job_manifest(spec, pending.env)
        agent = next(
            c for c in manifest["spec"]["template"]["spec"]["containers"] if c["name"] == "agent"
        )
        return {pair["name"]: pair["value"] for pair in agent["env"]}

    def teardown(self) -> None:
        for burst_id in self._spawned:
            subprocess.run(
                ["kubectl", "-n", self.runner.namespace, "delete", "job", job_name(burst_id),
                 "--ignore-not-found", "--wait=false"],
                capture_output=True,
            )
