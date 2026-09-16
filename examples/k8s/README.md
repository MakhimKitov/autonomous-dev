# Reference k8s manifests

What the platform looks like on a cluster — **an example, not a dependency**
(TO-BE §5: "reference k8s manifests and a compose file are examples"; no
orchestrator client exists in platform code). CI validates these with
`kubeconform` so they cannot rot; nothing deploys them.

Three files, two boundaries:

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
- **`burst-runner.yaml`** — where bursts run (G3): the `bursts` namespace, and
  the account whose whole grant is the four verbs `JobsRunner` speaks (create a
  Job, read the pod and its log, delete the Job to fence a burst), namespaced.
  The pods it creates hold no account at all.

**Bursts on a cluster are G3, and it has landed.** Set `BURST_NAMESPACE` on the
replicas and `open_runner` returns `JobsRunner`: one Job per burst, three
containers, no service-account token in the pod. Leave it unset and the control
plane runs bursts through `DockerRunner`, which needs a docker socket a pod does
not have — so an unset deployment on a cluster is the *coordination* tier
(observe, claim, wake, STOP) with bursts on a Docker host (TO-BE §6), which is
what these manifests were before G3 and still support.

Placeholders are `REPLACE_ME`; everything else is a real default.

```bash
docker run --rm -v "$PWD/examples/k8s:/m:ro" ghcr.io/yannh/kubeconform:v0.6.7 -strict -summary /m
kubectl apply -f examples/k8s/    # on a cluster of yours, after replacing the placeholders

# and a throwaway cluster that runs bursts, for the contract suite (G3):
./scripts/burst-cluster.sh
```
