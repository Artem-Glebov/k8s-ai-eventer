# ai-k8s-eventer

[![Buy Me a Coffee](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://www.buymeacoffee.com/artem.glebov)

In-cluster Kubernetes event tracker with a local, CPU-only LLM (Ollama) and a Streamlit UI. Watches cluster Events, runs deterministic health checks (resource requests/limits, probes, crash/OOM/backoff patterns), and turns them into human-readable advice for user-defined "watch targets" — plain-language instructions like "this is the backend, tell me if it's healthy."

No data leaves the cluster: the LLM runs in-cluster against Ollama, never an external API.

## Features

- **Per-target monitoring:** Define "watch targets" (a Deployment/StatefulSet/DaemonSet + plain-language instruction); the rule engine + LLM produce a Status/Issues/Recommendation insight refreshed on an interval or on-demand.
- **Deterministic rule checks:** Missing resource requests/limits, missing liveness/readiness probes, high restart count, CrashLoopBackOff/ImagePullBackOff/OOMKilled, untagged/`:latest` images, missing HPA/PDB, node pressure (cluster-scope only) — all with grounded, ready-to-apply remediation (real YAML snippets / kubectl commands computed from real cluster values, never LLM-invented).
- **Cluster-wide sweep ("Creative Mode"):** Optional periodic unscoped sweep of every workload in scope, catching problems outside your configured watch targets.
- **Ad-hoc chat:** Freeform chat page for asking questions about current cluster state, grounded in the same rule findings/events.
- **Access control:** Login-gated Streamlit UI (bcrypt + signed cookie), auto-generated bootstrap admin password on first install, per-user account management page.
- **Email notifications:** Agent acts as an SMTP client with debounced Critical-transition alerts (your own mail server/relay — this project never runs one).
- **Helm chart with configurable RBAC scope:** Day-one choice between cluster-wide (`cluster`) or least-privilege per-namespace (`namespaces`) monitoring, no hardcoded StorageClass dependencies.

## Architecture

Single agent process (`services/agent/`) runs the Events watch, rule engine, LLM analyzer, and read-only FastAPI, all in one Deployment with one SQLite writer (WAL mode). A separate Streamlit UI (`services/ui`) talks only to the agent over HTTP, never touches SQLite directly — this single-writer pattern avoids race conditions and keeps the architecture simple. An in-cluster Ollama deployment serves all LLM inference. The agent Deployment uses `strategy: Recreate` (never run two agent pods at once, since there's a single SQLite writer). Deterministic rule checks produce a fixed fact list that's always shown to the LLM as immutable truth, with a fixed JSON output template and low temperature — this is the main defense against the small CPU-only model hallucinating or inventing remediations.

## Prerequisites

**For local development:**
- Docker
- kubectl
- Helm v3
- minikube

**For real deployment:**
- Any conformant Kubernetes cluster (EKS, self-hosted, etc.)
- A container registry you can push images to

## Local Development

Local dev/test runs against a dedicated minikube profile (`ai-eventer`), isolated from any other cluster/context on your machine. The cluster deliberately uses vanilla Kubernetes (not k3s) so the chart doesn't depend on distro-specific defaults (e.g. k3s's bundled Traefik or `local-path` StorageClass) — this was validated live on k3d/k3s with `rbac.scope: namespaces` and confirmed working identically (see `docs/phase5-hardening-portability.md`).

```sh
make cluster-up      # starts the ai-eventer minikube profile (vanilla k8s, metrics-server enabled)
make deploy           # docker build agent+ui images, minikube image load, helm upgrade --install
make logs-agent       # tail agent logs
make logs-ui          # tail Streamlit UI logs
make undeploy         # helm uninstall
make cluster-down     # delete the minikube profile
make kubeconfig-win   # flatten kubeconfig for Lens (re-run after every cluster-up)
```

After first install, retrieve the generated admin password and access the UI:

```sh
kubectl -n ai-eventer get secret ai-eventer-auth -o jsonpath='{.data.admin-password}' | base64 -d
kubectl -n ai-eventer port-forward svc/ai-eventer-ui 8501:8501
# Open http://localhost:8501 and log in with username "admin" and the password above
```

## Deploying to a Real Cluster

### Building and Pushing Images

The `make push` target builds images and pushes them to your registry (deployment details are yours to decide):

```sh
make push REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com/ai-eventer TAG=v0.1.0
```

This only builds and pushes; it prints the exact `helm upgrade --install` command to run afterward, e.g.:

```sh
helm upgrade --install ai-eventer ./chart --kube-context <your-context> --namespace ai-eventer --create-namespace \
  --set agent.image.repository=<account>.dkr.ecr.<region>.amazonaws.com/ai-eventer/ai-eventer-agent \
  --set agent.image.tag=v0.1.0 \
  --set agent.image.pullPolicy=IfNotPresent \
  --set ui.image.repository=<account>.dkr.ecr.<region>.amazonaws.com/ai-eventer/ai-eventer-ui \
  --set ui.image.tag=v0.1.0 \
  --set ui.image.pullPolicy=IfNotPresent
```

### RBAC Scope

Default is `rbac.scope: cluster` (watches all Events and workloads cluster-wide). On a shared cluster you don't fully own, override to `rbac.scope: namespaces` with `watchNamespaces: [...]` for least-privilege monitoring of specific namespaces only:

```yaml
rbac:
  scope: namespaces
watchNamespaces:
  - default
  - production
```

Note: Node-pressure checks only work under `scope: cluster` (nodes are cluster-scoped resources inaccessible to per-namespace RoleBindings). HPA/PDB checks work under both scope modes.

### Storage

`storageClassName` defaults to `""` (cluster's default StorageClass), which works automatically on EKS's gp2/gp3-backed default. Override only if you need a specific storage class:

```yaml
storageClassName: ebs-sc  # e.g., for a custom EBS-backed class
```

### Exposing the UI Externally

Off by default (`ingress.enabled: false`). Use `kubectl port-forward` for non-internet-facing access. To expose externally, set:

```yaml
ingress:
  enabled: true
  className: alb  # or nginx, Traefik, etc. — any ingress controller
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing  # ALB-specific
  hosts:
    - host: eventer.example.com
      paths:
        - path: /
          pathType: Prefix
```

The chart deliberately ships no opinion on which ingress controller you use — only a generic `networking.k8s.io/v1` Ingress template.

### Security Posture

- Agent and UI containers run as non-root (uid 1000), all workloads have `readOnlyRootFilesystem: true` and all Linux capabilities dropped.
- Credentials (admin bootstrap password, cookie signing key) are auto-generated into a Secret on first install and never appear in `values.yaml`.
- SMTP credentials must come from a Secret you create yourself (never inline in values): `kubectl create secret generic ai-eventer-smtp --from-literal=username=... --from-literal=password=...`
- Full security details: `docs/phase5-hardening-portability.md`.

### First-Install Timing

The post-install/post-upgrade `ollama-pull` Job downloads the configured model (default `llama3.2:3b`, ~2GB) plus the `ollama/ollama` image itself before LLM analysis can begin. Budget several minutes for a completely cold install on a fresh cluster/registry. The agent degrades gracefully (`status: Unknown`) on analyzer ticks before the model is ready, so this isn't a crash, just a startup delay.

### Minimum Sizing Guidance

Sum of default resource *requests* across all components is roughly 2.25 CPU / 2.3Gi memory (dominated by Ollama at 2 CPU/2Gi — it's the only component doing real inference work, entirely CPU-only, no GPU support). Tune `ollama.resources` up if you have headroom (this is the main lever for first-token latency), or down only if you accept slower responses. See `docs/ad-hoc-chat-interface.md` for actual latency measurements and cache behavior.

## Configuration Reference

See `chart/values.yaml` (inline comments for every option) and `chart/values.schema.json` (validated shape, catches typos at `helm lint` time) for the full reference. Key config areas:

| Setting | Purpose | Default |
|---------|---------|---------|
| `rbac.scope` | `cluster` or `namespaces` | `cluster` |
| `watchNamespaces` | Namespaces to monitor (if `scope: namespaces`) | `[]` |
| `storageClassName` | Kubernetes StorageClass for SQLite PVC | `""` (cluster default) |
| `agent.analyzeIntervalSeconds` | How often to re-evaluate rules/call LLM per target | `180` |
| `creativeMode.enabled` | Enable unscoped cluster-wide sweep | `true` |
| `creativeMode.intervalSeconds` | Interval for cluster-wide sweep | `600` |
| `ingress.enabled` | Expose UI via Ingress (requires ingress controller) | `false` |
| `auth.cookieExpiryDays` | Session cookie lifetime | `30` |
| `notifications.smtp.enabled` | Send email alerts on Critical transitions | `false` |
| `notifications.smtp.existingSecretName` | Secret with `username` and `password` keys | `""` |
| `ollama.model` | Model to pull and use (via `ollama pull`) | `llama3.2:3b` |
| `ollama.resources` | CPU/memory for inference server | `{requests: {cpu: 2, memory: 2Gi}, limits: {cpu: 4, memory: 5Gi}}` |
| `watchTargets` | Default watch targets seeded on install | Example backend Deployment |

## Feature Deep-Dives

- **[Event Matching Widened to Pods](docs/event-matching-widened-to-pods.md):** How pod-level events from Kubernetes are matched and surfaced to the UI and LLM alongside Deployment-level events.
- **[HPA, PDB, and Node Pressure Checks](docs/hpa-pdb-node-pressure-checks.md):** Deterministic checks for missing HorizontalPodAutoscaler, PodDisruptionBudget, and node resource pressure; includes status severity mapping (Critical vs. Degraded).
- **[Grounded Remediation](docs/grounded-remediation.md):** How every finding includes a concrete, ready-to-apply suggested fix (real YAML/kubectl commands) that grounds the LLM's recommendation and prevents hallucination.
- **[Ad-Hoc Chat Interface](docs/ad-hoc-chat-interface.md):** Freeform cluster-state Q&A with real latency measurements, CPU-only inference ceiling, and prompt-prefix-cache behavior.
- **[Creative Mode: Cluster-Wide Sweep](docs/creative-mode-cluster-sweep.md):** Periodic unscoped scan of every workload in scope, catching unknown-unknowns outside configured watch targets.
- **[StatefulSet and DaemonSet Support](docs/statefulset-daemonset-support.md):** Extended rule engine beyond Deployment to cover StatefulSet and DaemonSet workloads.
- **[Access, Account, and Notifications](docs/access-account-notifications.md):** Login-gated UI, user account management, and SMTP-based email alerts on Critical transitions.
- **[Phase 5: Hardening and Portability Validation](docs/phase5-hardening-portability.md):** JSON Schema validation, security hardening (non-root, read-only filesystems, dropped capabilities), and live cross-distro testing (k3d/k3s).

## License

MIT — see `LICENSE`.
