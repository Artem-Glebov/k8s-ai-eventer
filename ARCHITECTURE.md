# Architecture

This document describes what this project is, how it's structured, and the reasoning behind
its key design decisions, for anyone working on this codebase.

## What this is

In-cluster Kubernetes event tracker with a local, CPU-only LLM (Ollama) and a Streamlit UI.
It watches cluster Events, runs deterministic health checks (resource requests/limits, probes,
crash/OOM/backoff patterns), and turns them into human-readable advice for user-defined "watch
targets" (plain-language instructions like "this is the backend, tell me if it's healthy").

No data leaves the cluster: the LLM only ever talks to an in-cluster Ollama, never an external API.

**Portability matters more than the local dev target.** Local development happens on minikube
against vanilla Kubernetes (not k3s), but real deployment targets are EKS and self-hosted
clusters — every chart default (StorageClass, RBAC) is chosen to avoid depending on any one
distro's defaults (e.g. k3s's bundled Traefik or `local-path` StorageClass).

## Commands

```sh
make cluster-up      # starts the dedicated `ai-eventer` minikube profile (vanilla k8s, metrics-server enabled)
make deploy           # docker build agent+ui images, minikube image load, helm upgrade --install
make logs-agent       # tail agent (collector + rules + analyzer + API) logs
make logs-ui          # tail Streamlit UI logs
make undeploy         # helm uninstall
make cluster-down     # delete the minikube profile
make kubeconfig-win   # flatten kubeconfig certs to a Windows-path copy for Lens (re-run after every cluster-up, since minikube's docker driver picks a new API server port each time)
```

All `make` targets default to the `ai-eventer` minikube profile/context/namespace — this is a
profile dedicated to this project, isolated from any other cluster/context already configured
on the machine. Never rely on or mutate the global current kubectl context; pass
`--context ai-eventer` explicitly if running kubectl/helm by hand.

There is no automated test suite or linter configured in this repo yet.

## Architecture

**One repo, one backend process.** `services/agent` runs the Events watch, the rule engine, the
LLM analyzer, and a read-only FastAPI, all in a single Deployment/process (`services/agent/main.py`
starts a watch thread, an analyzer thread, and serves FastAPI on the main thread). `services/ui`
is a separate Streamlit app that only talks to that FastAPI over HTTP — it never touches the
SQLite file directly. This is deliberate: a single SQLite writer avoids the risk of multiple
pods writing the same file.

- `watch.py` — one daemon thread per watched namespace (or one for `scope: cluster`) doing a
  Kubernetes `list+watch` on Events. On any stream error it backs off and does a full relist
  rather than tracking `resourceVersion` — simpler/more robust for a solo-maintained loop, and a
  stale replay of already-seen events is harmless for advice generation. `WATCH_TIMEOUT_SECONDS`
  also forces a periodic reconnect so the relist path is exercised in normal operation.
- `rules.py` — deterministic checks (missing resource requests/limits, missing liveness/readiness
  probes, high restart count, `CrashLoopBackOff`/`ImagePullBackOff`/`OOMKilled`) that produce a
  fixed fact list. MVP scope: a watch target selects a single Deployment by name; pods are then
  found via the Deployment's own label selector. Other selector kinds are a later expansion.
- `llm.py` — **facts-then-narrative prompting**: `rules.py` findings + aggregated recent event
  counts are computed first and handed to the model as a fact list it's told never to contradict,
  with a fixed JSON output template (`status`/`issues`/`recommendation`), low temperature, and a
  capped token budget. This is the main defense against the small CPU-only model
  (`llama3.2:3b`) hallucinating or rambling. On any call failure or malformed JSON, `analyze()`
  degrades to `status: "Unknown"` rather than raising — the next analyzer tick tries again.
- `db.py` — single SQLite connection in WAL mode, all writes serialized behind one `threading.Lock`
  (`write()` context manager) so the watch loop, analyzer loop, and FastAPI reads can share one
  file without separate writer processes.
- `api.py` — read-only FastAPI surface (`/targets`, `/events`, `/findings`, `/insights/latest`)
  that the UI polls; it never writes.
- `main.py` — wires everything together: loads in-cluster kube config, seeds watch targets from
  the mounted `targets.yaml` ConfigMap on startup, then runs the analyzer loop on an interval
  (evaluate rules → call the LLM → persist an insight → prune old rows) per watch target.

**Helm chart (`chart/`)** deploys three components — agent, Streamlit UI, and Ollama (with a
post-install/post-upgrade hook Job that blocks on `ollama pull` until the model is available;
`llm.py` handles the case where the analyzer runs before the pull finishes).

- **RBAC scope is a day-one config choice** (`values.yaml` → `rbac.scope: cluster | namespaces`
  + `watchNamespaces`): the same `ClusterRole` is always defined once; only the binding differs
  — a single `ClusterRoleBinding` for cluster scope, or one `RoleBinding` per namespace in
  `watchNamespaces` otherwise (see `chart/templates/rbac.yaml`). Node visibility only takes
  effect under `scope: cluster`, since a per-namespace `RoleBinding` can't grant access to the
  cluster-scoped `nodes` resource — that's intentional least-privilege behavior, not a bug.
  Corresponding env vars on the agent Deployment: `RBAC_SCOPE`, `WATCH_NAMESPACES`.
  `watch.py`'s `start()` reads these to decide whether to spawn one watch thread for the whole
  cluster or one per namespace.
- The agent Deployment uses `strategy: Recreate` — never run two agent pods at once, since
  there's a single SQLite writer.
- Default watch targets live in `values.yaml` → `watchTargets`, rendered into a ConfigMap
  (`configmap-targets.yaml`) and seeded into SQLite on agent startup (`main.py`'s `seed_targets`)
  — a cluster rebuild recreates known state.
