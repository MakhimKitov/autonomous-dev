#!/usr/bin/env bash
# A throwaway cluster to run bursts on (G3, #44) — what `JobsRunner`'s half of
# `tests/test_runner_contract.py` needs, and what the Stage G done-when is
# rehearsed against. Idempotent: run it again and it repairs what is missing.
#
#   ./scripts/burst-cluster.sh            # build it, print the exports
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

echo "==> the namespace and the runner's account"
kubectl --context "$ctx" apply -f "$ROOT/examples/k8s/burst-runner.yaml" >/dev/null

cat <<EXPORTS

The cluster is up. Point a shell at it:

  export KUBECONFIG=\${KUBECONFIG:-~/.kube/config}
  kubectl config use-context $ctx
  export BURST_NAMESPACE=$NAMESPACE
  export ARCHIVE_URL='s3://platform:platform-dev-secret@$host_ip:8333/$BUCKET?tls=false'
  export BURST_PHASES_IMAGE=$PHASES_IMAGE

  uv run pytest tests/test_runner_contract.py tests/test_jobs_runner.py

EXPORTS
