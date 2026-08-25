# Phase 5: Hardening and Portability Validation

## What Changed

Phase 5 closes the final phased hardening effort. The checklist was originally (a) Basic auth or NetworkPolicy in front of the Streamlit Service, and (b) no portability-breaking assumptions specific to minikube's environment. Both are now satisfied.

### 1. Basic Auth Checklist Item — Already Satisfied

The "Basic auth or NetworkPolicy" requirement is already closed by the pre-existing Access feature (documented in `docs/access-account-notifications.md`) — a Streamlit login gate with bcrypt-hashed passwords stored in SQLite, deployed by the Helm chart on every install. This satisfied the checklist's intent (gate human-facing UI from unauthenticated access) and was the existing implementation choice; no new work was needed here.

The optional second half of the original checklist (NetworkPolicy in front of the Streamlit Service) remains deferred — it would be a defense-in-depth addition but is not required, since the login gate already controls access.

### 2. Helm Values Schema Validation (`chart/values.schema.json`)

A new JSON Schema file was added to enforce constraints on `chart/values.yaml` at lint time. This catches configuration errors before Helm attempts to render and deploy.

**Schema coverage:**

- **Enum enforcement:** `rbac.scope` must be `"cluster"` or `"namespaces"` (prevents typos like `"clusters"` silently rendering a broken RoleBinding/ClusterRoleBinding pair).
- **Required fields:** each of `agent`, `ui`, `ollama`, `auth` sections must be present; image names, repository, pull secrets, resources shapes all required with no defaults implied.
- **Watch target validation:** each target's `selector.kind` must be one of `"deployment"`, `"statefulset"`, or `"daemonset"` (enforced before a typo crashes the analyzer loop).
- **Shared `$ref` definitions:** image configuration, resource requests/limits, and service port shapes are defined once and reused across multiple places in the schema (DRY principle for maintainability).

The schema is evaluated automatically by `helm lint` when run during `make deploy` or manual `helm lint chart/` — no separate validation tool needed.

### 3. Resource Limits on the Ollama Pull Job

The `chart/templates/ollama-pull-job.yaml` container lacked a `resources` block. Every other workload in the chart (agent Deployment, UI Deployment, ollama server Deployment) already had resource requests and limits set. This gap was inconsistent.

Added a new `ollama.pullJob.resources` configuration block in `chart/values.yaml` (defaults: `requests: {cpu: 50m, memory: 64Mi}`, `limits: {cpu: 200m, memory: 128Mi}`). This is appropriately small — the pull job is just an HTTP client call to the already-resourced ollama server, not inference itself — so it needs far less than the full model server.

### 4. Cross-Distro Portability Verification on k3d

The project's documented portability goal is: *local development targets vanilla Kubernetes (minikube), real deployments target EKS and self-hosted clusters. The chart must not depend on any one distro's defaults (e.g., k3s's bundled Traefik or `local-path` StorageClass).* This was tested in practice for the first time.

**Test setup:** Created a temporary `k3d` cluster (`ai-eventer-portability`, using k3s — deliberately different from the primary minikube dev target, with its own bundled Traefik and `local-path` StorageClass). Imported pre-built `ai-eventer-agent:dev` and `ai-eventer-ui:dev` images. Installed the chart with `--set rbac.scope=namespaces --set watchNamespaces='{default}'` — the namespace-scoped RBAC path had never run against a live cluster before, only validated via `helm template`.

**Live verification:**

1. **StorageClass binding** — PVC for SQLite automatically bound to k3d's `local-path` StorageClass with zero hardcoded StorageClass name in the chart (uses default, allowing each cluster to provide its own). Confirmed binding succeeded in both namespace-scoped and cluster-scoped RBAC modes.

2. **RBAC correctness under `rbac.scope: namespaces`** — the chart correctly created a `RoleBinding` (not `ClusterRoleBinding`) in the `default` namespace only, with no cluster-scoped bindings elsewhere. Verified via `kubectl get rolebindings,clusterrolebindings -A | grep ai-eventer`, confirming RBAC is minimally scoped as intended.

3. **Node visibility gating** — confirmed the agent's real in-cluster ServiceAccount token received a genuine `403 Forbidden` calling `list_node()` via the actual Python kubernetes client. This proves node visibility is correctly cut off under `scope: namespaces`, matching the project's documented least-privilege design. (Methodology note: `kubectl auth can-i list nodes --as=<sa>` was initially used but returned misleading "yes" — kubectl silently attaches the current context's namespace to SelfSubjectAccessReview for cluster-scoped resources. The ground truth came from an explicit impersonation `SelfSubjectAccessReview` and from the real in-pod client call, both correctly returning `allowed: false` / `403`.)

4. **Post-install hook** — the `ollama-pull` Job ran successfully, pulled `llama3.2:3b`, and self-deleted per `hook-delete-policy: hook-succeeded`, identical to behavior on minikube. The chart's hook annotations are cluster-agnostic and work correctly on both.

5. **End-to-end functional pipeline** — deployed a deliberately misconfigured test Deployment (`broken-test` with bad image, no resources/probes/HPA/PDB) into the watched `default` namespace, registered it as a watch target via the API, triggered analysis, and verified correct deterministic findings (ImagePullBackOff, missing requests/limits/probes/HPA/PDB), correct grounded remediation, correct severity computation (`Critical` status), and a clean non-hallucinated LLM recommendation — identical behavior to the primary minikube target.

**Operational note on cold-start:** The very first `helm install --wait` timed out (5-minute deadline) because `ollama/ollama:latest` (~3.4GB) had to be pulled fresh from Docker Hub (unlike agent/ui images, which were pre-imported). A follow-up `helm upgrade` re-attempted hook execution and succeeded immediately since the image was now cached. Not a chart bug — just expected behavior on a real cluster's first install; operators should budget for image pull time.

**Cleanup:** Test Deployment, Helm release, and the entire `k3d` cluster were deleted afterward. The temporary `ai-eventer-auth` Secret survived until cluster deletion (kept alive during the run via `helm.sh/resource-policy: keep`). The primary `ai-eventer` minikube cluster was untouched; `kubectl` context was explicitly restored afterward.

## Why This Design, and What Was Rejected

**Schema validation before deploy, not error discovery at runtime:**
A typo in `rbac.scope` or `selector.kind` would previously render silently into a broken manifest and fail only when Kubernetes tried to reconcile it (RBAC binding with no subjects, analyzer crash on undefined selector kind). Pre-validating at lint time — a simple, free operation — catches these user errors immediately and clearly. The schema is checked before the first pod ever starts.

**Namespace-scoped RBAC gating on node visibility, not try/except:**
The chart could have attempted node reads and caught `403 Forbidden` on `rbac.scope: namespaces`. Instead, node-check logic is gated on scope in the Python code itself (see `rules.py`), mirroring the project's established pattern in `api.py`. Explicit scope checks and feature gates beat implicit error swallowing for clarity and testability.

**Portability test on a second distro, not just minikube rendering checks:**
Helm templating succeeds even for manifests that would fail on a real cluster (missing StorageClass, network plugins, etc.). A live k3d run (a fundamentally different Kubernetes distro than minikube/docker driver) actually exercises the "no hardcoded defaults" goal and catches distro-specific assumptions that template checks miss. This is why a second-cluster run was necessary.

**Small resources on the pull job, not inherited from ollama server:**
The pull job's resource needs (50m/64Mi requests, 200m/128Mi limits) are orders of magnitude smaller than the inference server's (see `chart/values.yaml`). Inheriting the server's full allocation would waste cluster capacity during pull (only 1–2 minutes per install/upgrade). The job's small, dedicated sizing is appropriate for its actual workload.

## Verification

- **Schema validation:** Ran `helm lint chart/` under default `rbac.scope: cluster` (passes clean), then `--set rbac.scope=namespaces --set watchNamespaces='{default}'` (passes clean). Explicitly passed `--set rbac.scope=bogus` — lint now fails with a clear JSON schema error message instead of silently rendering a broken RoleBinding. Same test for `watchTargets[].selector.kind=cronjob` (unsupported kind) — lint fails clearly.

- **Resource limits on pull job:** `helm template chart/ --set ollama.pullJob.resources=null` confirmed the block is optional (defaults apply). `helm template` with the default values renders the `resources` block correctly with the stated requests/limits. `make deploy` to ai-eventer minikube confirmed the Job pod runs and completes without resource constraint errors.

- **Cross-distro k3d run:** Full end-to-end as described above — StorageClass binding, RBAC scoping, node permission gating, hook execution, functional pipeline all verified live. No chart changes were needed; the existing code ran unchanged on a different Kubernetes distro, confirming portability goal is achieved.

## What to Know for Maintenance

- **Schema is the source of truth for allowed values.** If a new top-level config block is added (e.g., `prometheus:` for metrics), update `chart/values.schema.json` to match. The schema doubles as documentation for operators and catches configuration errors automatically.

- **`rbac.scope: namespaces` has no node visibility.** This is intentional least-privilege behavior, not a bug. Node-pressure checks never fire under namespace scope — operators should understand this when choosing their RBAC scope.

- **First `helm install` on a cluster with no cached `ollama/ollama:latest` image will take several minutes.** The post-install hook blocks on `ollama pull`; if the image isn't already cached, the pull from Docker Hub can take 5+ minutes depending on network. `helm install --wait` with default timeout (5 minutes) may hit deadline, but the image is left cached and `helm upgrade` will succeed immediately. This is expected and not a failure.

- **The pull job's resources are separate from the inference server's.** Adjusting `ollama.resources` (the server's allocation) does not affect `ollama.pullJob.resources`. They can be tuned independently — cluster admins may want a small pull job and large server, or vice versa.

- **Phase 5 (hardening) is complete.** This closes the final phase from the original plan. All phased architecture/stability/access/hardening goals have been implemented and verified. Future improvements (NetworkPolicy, OIDC integration, Slack notifications, etc.) are deferred as separate feature work, not part of the hardening phase.
