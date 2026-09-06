# Reference k8s manifests

What the platform looks like on a cluster — **an example, not a dependency**
(TO-BE §5: "reference k8s manifests and a compose file are examples"; no
orchestrator client exists in platform code). CI validates these with
`kubeconform` so they cannot rot; nothing deploys them.

Two workloads, one boundary:

- **`control-plane.yaml`** — the replicas (`Deployment`, 2 stateless peers over
  one ledger) and the *internal* wake `Service` on 8081. Cluster network only;
  no `Ingress`. `automountServiceAccountToken: false` — a replica has no
  business with the k8s API (that arrives with `JobsRunner`, G3, in its own
  scoped account).
- **`webhook-receiver.yaml`** — the one public surface: `Deployment`, `Service`,
  the `Ingress` that is E6's HTTPS route, and a `NetworkPolicy` that lets the
  receiver talk to exactly one thing — the control plane's wake port — and be
  reached only on its own. The webhook secrets are `file:` refs onto a mounted
  `Secret`; the image holds nothing.

**Bursts on a cluster are Stage G.** Until `JobsRunner` (#44) the control plane
runs bursts through `DockerRunner`, which needs a docker socket a pod does not
have — so these manifests deploy the *coordination* tier (observe, claim, wake,
STOP) and the T0 deployment that runs bursts is the compose file's `platform`
profile on a Docker host (TO-BE §6). The manifests exist now so the receiver +
replicas shape is pinned and validated before G3 fills in the runner.

Placeholders are `REPLACE_ME`; everything else is a real default.

```bash
docker run --rm -v "$PWD/examples/k8s:/m:ro" ghcr.io/yannh/kubeconform:v0.6.7 -strict -summary /m
kubectl apply -f examples/k8s/    # on a cluster of yours, after replacing the placeholders
```
