#!/usr/bin/env bash
# A throwaway cluster to run bursts on (G3, #44) — what `JobsRunner`'s half of
# `tests/test_runner_contract.py` needs, and what the Stage G done-when is
# rehearsed against. Idempotent: run it again and it repairs what is missing.
#
#   ./scripts/burst-cluster.sh            # build it, print the exports
#   ./scripts/burst-cluster.sh --gvisor   # ...and a second RuntimeClass, `gvisor` (G4)
#   ./scripts/burst-cluster.sh --delete   # take it away
#
# Two things here are not obvious, and both are about a laptop rather than about
# k8s:
#
#   * **one address for the archive, from both sides.** A presigned URL is signed
#     over its Host header, so the control plane (on this host) and the pod (in
#     the cluster) have to name the object store identically. Container IPs are
#     not routable from macOS and `localhost` means something different inside a
#     pod — but this host's own LAN address works from both, so that is what the
#     archive URL is built from.
#   * **no registry.** The phases image is loaded into the node with `kind load`,
#     and the contract suite's bursts run that same image (their "agent" is a
#     shell script), so the cluster needs exactly one artifact.
set -euo pipefail

CLUSTER="${BURST_CLUSTER:-autonomous-dev}"
NAMESPACE="${BURST_NAMESPACE:-bursts}"
BUCKET="${BURST_BUCKET:-burst-cluster}"
PHASES_IMAGE="${BURST_PHASES_IMAGE:-autonomous-dev/burst-phases:latest}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The golden replay's second runtime (G4): gVisor, pinned and checksummed. It is
# the candidate a kind node can carry — Kata wants nested virtualization, which
# neither a laptop's VM nor a CI runner offers — so the harness is proven here
# on two runtimes, and the bake-off proper (G5) runs it on hardware with all three.
GVISOR_RELEASE="${BURST_GVISOR_RELEASE:-20260921.0}"

if [[ "${1:-}" == "--delete" ]]; then
  kind delete cluster --name "$CLUSTER"
  docker rm -f burst-archive >/dev/null 2>&1 || true
  exit 0
fi

for tool in kind kubectl docker; do
  command -v "$tool" >/dev/null || { echo "$tool is not on PATH" >&2; exit 1; }
done

host_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I | awk '{print $1}')"
[[ -n "$host_ip" ]] || { echo "could not find this host's LAN address" >&2; exit 1; }

if ! kind get clusters | grep -qx "$CLUSTER"; then
  echo "==> creating the cluster"
  kind create cluster --name "$CLUSTER"
fi
ctx="kind-$CLUSTER"

echo "==> the archive, published where both sides can reach it"
if ! docker ps --format '{{.Names}}' | grep -qx burst-archive; then
  docker rm -f burst-archive >/dev/null 2>&1 || true
  docker run -d --name burst-archive -p 8333:8333 \
    -v "$ROOT/docker/seaweedfs/s3.json:/etc/seaweedfs/s3.json:ro" \
    chrislusf/seaweedfs:4.41 \
    server -dir=/data -master.volumeSizeLimitMB=256 -s3 -s3.port=8333 \
    -s3.config=/etc/seaweedfs/s3.json >/dev/null
fi

echo "==> building and loading the phases image"
docker build -q -f "$ROOT/docker/burst-phases/Dockerfile" -t "$PHASES_IMAGE" "$ROOT" >/dev/null
kind load docker-image --name "$CLUSTER" "$PHASES_IMAGE"
if [[ -n "${BURST_AGENT_IMAGE:-}" ]]; then
  # the golden replay's agent: the real dev-agent image, built beforehand
  kind load docker-image --name "$CLUSTER" "$BURST_AGENT_IMAGE"
fi

if [[ "${1:-}" == "--gvisor" ]]; then
  echo "==> gVisor $GVISOR_RELEASE in the node, as RuntimeClass gvisor"
  node="$CLUSTER-control-plane"
  if ! docker exec "$node" test -x /usr/local/bin/runsc; then
    # unpacked here, not in the node: the node image has no bzip2
    arch="$(docker exec "$node" uname -m)"
    base="https://storage.googleapis.com/gvisor/releases/release/$GVISOR_RELEASE/$arch"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    curl -fsSL -o "$work/gvisor.tar.bz2" "$base/gvisor.tar.bz2"
    curl -fsSL -o "$work/gvisor.tar.bz2.sha512" "$base/gvisor.tar.bz2.sha512"
    (cd "$work" && shasum -a 512 -c gvisor.tar.bz2.sha512 >/dev/null)
    mkdir "$work/bin" && tar -xjf "$work/gvisor.tar.bz2" -C "$work/bin"
    docker cp "$work/bin/." "$node:/usr/local/bin/"
  fi
  if ! docker exec "$node" grep -q "runtimes.runsc" /etc/containerd/config.toml; then
    docker exec "$node" sh -c 'cat >> /etc/containerd/config.toml <<TOML

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
TOML
systemctl restart containerd'
  fi
  kubectl --context "$ctx" apply -f "$ROOT/examples/k8s/runtimeclass-gvisor.yaml" >/dev/null
  kubectl --context "$ctx" wait --for=condition=Ready node --all --timeout=120s >/dev/null
fi

echo "==> the namespace and the runner's account"
kubectl --context "$ctx" apply -f "$ROOT/examples/k8s/burst-runner.yaml" >/dev/null

cat <<EXPORTS

The cluster is up. Point a shell at it:

  export KUBECONFIG=\${KUBECONFIG:-~/.kube/config}
  kubectl config use-context $ctx
  export BURST_NAMESPACE=$NAMESPACE
  export ARCHIVE_URL='s3://platform:platform-dev-secret@$host_ip:8333/$BUCKET?tls=false'
  export BURST_PHASES_IMAGE=$PHASES_IMAGE
  export BURST_REPLAY_RUNTIMES=$([[ "${1:-}" == "--gvisor" ]] && echo default,gvisor || echo default)

  uv run pytest tests/test_runner_contract.py tests/test_jobs_runner.py tests/test_replay.py

EXPORTS
