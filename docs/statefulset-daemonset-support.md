# StatefulSet and DaemonSet Support

## What Changed

The rule engine in `services/agent/rules.py` and the watch-target concept were extended from supporting only `selector_kind == "deployment"` to also support `statefulset` and `daemonset` — the three most common Kubernetes workload kinds — for both per-target analysis and the cluster-wide "creative mode" sweep (see `docs/creative-mode-cluster-sweep.md`).

### Implementation

- **`KIND_INFO` lookup table** — new module-level dict mapping `selector_kind` string (`"deployment"`, `"statefulset"`, `"daemonset"`) to the K8s Kind string and exact `AppsV1Api` method names (`read_namespaced_*`, `list_namespaced_*`, `list_*_for_all_namespaces`). `SUPPORTED_SELECTOR_KINDS = set(KIND_INFO)` is exported and imported by `api.py`, ensuring the API layer and rule engine never drift on supported kinds.

- **Function rename and signature change:** `_evaluate_deployment(workload, namespace)` → `_evaluate_workload(workload, namespace, api_kind: str)`. The function now takes an explicit `api_kind` parameter (never read off the fetched object's `.kind` field, which is frequently empty on single-object GET responses and only reliably populated on LIST responses). Verified that `V1DeploymentSpec`, `V1StatefulSetSpec`, and `V1DaemonSetSpec` (from `kubernetes==31.0.0` client) all expose identical `.selector.match_labels` and `.template.spec.containers` shapes, so one function safely serves all three kinds.

- **HPA handling for DaemonSet:** `_hpa_finding()` now returns `None` (no finding) when `workload_kind == "DaemonSet"` — a DaemonSet has no `/scale` subresource and no replica-count concept; the Kubernetes Python client has no `*_daemon_set_scale` methods (present for Deployment/StatefulSet), confirming HorizontalPodAutoscaler can never target one. Without this exclusion, every DaemonSet would incorrectly and permanently show `missing_hpa`.

- **PDB and node-pressure checks:** `_pdb_finding()` and `_node_pressure_findings()` required no logic changes (already kind-agnostic, label-selector based). Detail/remediation text generalized from "this Deployment" → "this workload" so StatefulSet/DaemonSet targets aren't misdescribed in LLM narratives.

- **`evaluate_target(namespace, name, selector_kind)` signature change:** now takes `selector_kind` as an explicit parameter and dispatches via `KIND_INFO`. Defensively handles unrecognized `selector_kind` by returning an `unsupported_selector_kind` finding (new entry in `SEVERITY_BY_CHECK` at `"Critical"` severity) instead of raising — needed because `main.py`'s `seed_targets()` reads `selector.kind` from the `targets.yaml` ConfigMap with zero validation (only the `PUT /targets/{name}` endpoint validates), so a typo could otherwise crash the analyzer loop with an uncaught `KeyError` on every tick.

- **Cluster-wide sweep:** `evaluate_all_deployments()` → `evaluate_all_workloads()` now iterates through all three kinds. Returned findings dict keys changed from `"namespace/name"` to `"Kind:namespace/name"` (e.g., `"StatefulSet:test-app/broken-statefulset"`) because a Deployment and a DaemonSet can legitimately share a name in the same namespace — the old format would have silently merged findings under one key.

- **`services/agent/api.py`:** `PUT /targets/{name}` now validates `selector_kind` against `SUPPORTED_SELECTOR_KINDS` (was a hardcoded `!= "deployment"` check). Endpoint `GET /namespaces/{namespace}/deployments` renamed to `GET /namespaces/{namespace}/workloads` with optional `kind` query param (default `"deployment"`); this endpoint's only caller is the Streamlit UI, so no external/backwards-compatibility concern.

- **`services/ui/Targets.py`:** the "Manage watch targets" form gained a "Kind" selectbox (Deployment/StatefulSet/DaemonSet) before the namespace field. It feeds both the workload-name dropdown (via the renamed `/workloads` endpoint with `kind` param) and the persisted `selector_kind` on save (previously hardcoded to `"deployment"`).

- **`services/ui/pages/2_Cluster_Overview.py`:** wording generalized from "Deployment" to "workload (Deployment/StatefulSet/DaemonSet)" in the cost caption, scan spinner, and result caption. Still reads the same underlying `deployments_scanned` SQLite column — only the display text changed.

- **`services/agent/llm.py`:** cluster-wide prompt (`CLUSTER_SYSTEM_PROMPT`, `build_cluster_prompt()`, `analyze_cluster()`) had all "Deployment"-specific wording and parameter names (`findings_by_deployment`, `deployments_scanned`) generalized to `findings_by_workload`/`workloads_scanned`. Per-target prompt (`SYSTEM_PROMPT`, `build_prompt()`, `analyze()`) required no changes — it never mentioned "Deployment".

- **`services/agent/db.py`:** `insert_cluster_insight()` Python parameter renamed `deployments_scanned` → `workloads_scanned` for readability. The backing SQLite column `cluster_insights.deployments_scanned` was deliberately left unrenamed — renaming a live column requires a migration purely for cosmetics, not worth it for an internal storage detail. A comment was added on the `CREATE TABLE cluster_insights` block noting this.

- **RBAC:** `chart/templates/rbac.yaml`'s `ClusterRole` `apps` apiGroup `resources` list gained `statefulsets`/`daemonsets` alongside the existing `deployments`/`replicasets` (verbs unchanged: `get`, `list`, `watch`). No `ClusterRoleBinding`/`RoleBinding` template change needed; both already bind the same `ClusterRole` regardless of `rbac.scope`. This went through Plan Mode per the project's rule that RBAC-touching changes must never be direct edits. Note: `replicasets` was already present (leftover from planned but deferred ReplicaSet support) and wasn't touched.

- **`chart/values.yaml`:** cosmetic comment added next to the example watch target's `selector.kind: deployment` line noting the other supported kinds.

- **Test fixtures** (gitignored dev fixture in `testapp/`): `broken-statefulset.yaml` (StatefulSet with no resources/limits/probes, pinned image tag — isolates `missing_requests`/`missing_limits`/`missing_liveness_probe`/`missing_readiness_probe`/`missing_hpa`/`missing_pdb`) and `broken-daemonset.yaml` (DaemonSet with an untagged image — isolates the same checks minus `missing_hpa`, which structurally can never fire for a DaemonSet).

## Why This Design, and What Was Rejected

**Deterministic rule engine generalization, not "LLM reads YAML freely":**
The user was asked directly whether extending support should mean giving the LLM raw/free-form manifest YAML to read, versus extending the existing deterministic rule engine to more kinds. They explicitly chose to keep the established "deterministic facts, LLM only narrates" architecture (see `docs/grounded-remediation.md` and the project's repeated hallucination-fighting history). This is a mechanical generalization of an existing kind-specific implementation, not a new capability.

**`api_kind` as an explicit parameter, not read off the fetched object's `.kind` field:**
The Kubernetes object's `.kind` field is frequently empty on single-object GET responses and only reliably populated on LIST responses. Reading it would have silently broken the HPA `scaleTargetRef.kind` match logic. Passing `api_kind` explicitly from the caller is the only safe approach.

**Kind-prefixed keys in cluster-wide findings (`"Kind:namespace/name"`):**
A Deployment and a DaemonSet can share the same name in the same namespace. The old `"namespace/name"` format would have silently merged/overwritten findings from different kinds under a single key, corrupting the result. The Kind prefix guarantees uniqueness and clarity.

**HPA exclusion for DaemonSet, not error handling:**
Rather than wrapping HPA calls in try/except and treating a missing `/scale` as "no HPA found", the check is skipped entirely when `workload_kind == "DaemonSet"`. This follows the project's explicit pattern in `api.py` (scope-gated features with explicit conditional branches, not implicit error swallowing).

**Unsupported kind as a finding, not an exception:**
If `selector_kind` in `targets.yaml` is a typo and the analyzer crashes on every tick with an uncaught `KeyError`, the problem is invisible in a running system until someone checks logs. Returning an `unsupported_selector_kind` finding instead makes the error visible in the API/UI and still allows other watch targets to analyze normally.

## Verification

- `helm template ./chart --set rbac.scope=cluster` and `--set rbac.scope=namespaces` confirmed RBAC rules render correctly under both binding shapes.
- `make deploy` to the `ai-eventer` minikube profile; confirmed via `kubectl get pods` that rollout was clean with zero restarts/CrashLoopBackOff (this project has a standing lesson that clean Helm rollout can hide crashes; this check matters).
- Added watch targets for `broken-statefulset` (`selector_kind: statefulset`) and `broken-daemonset` (`selector_kind: daemonset`) via the API and ran "Analyze now" on each. Confirmed findings matched expectations: the StatefulSet showed `missing_requests`, `missing_limits`, `missing_liveness_probe`, `missing_readiness_probe`, `missing_hpa`, `missing_pdb`; the DaemonSet showed the same set minus `missing_hpa` (plus `latest_image_tag`, since its fixture used an untagged image). This confirms the DaemonSet HPA-skip logic works correctly in practice, not just in code review. Both LLM narratives correctly paraphrased the real findings with no hallucinated fixes and correctly referred to both as a generic "workload" in PDB-related text.
- Confirmed new `GET /namespaces/test-app/workloads?kind=statefulset` / `?kind=daemonset` / `?kind=deployment` endpoint returns the correct filtered name lists for each kind against the live cluster.
- Confirmed `PUT /targets/{name}` correctly rejects an unsupported `selector_kind` (tried `"cronjob"`) with an HTTP 400 listing valid kinds, rather than crashing.
- Triggered a live cluster-wide "Scan now" (`POST /cluster-insights/scan`) after the change: it correctly reported `workloads_scanned: 10` across `["ai-eventer", "test-app"]` (matching the real count of Deployments+StatefulSet+DaemonSet across both namespaces) with no crash and no key-collision — confirming `evaluate_all_workloads()`'s kind-prefixed keys work correctly end-to-end.

## What to Know for Maintenance

- `KIND_INFO` in `rules.py` is the single source of truth for supported selector kinds. Adding a new kind requires updating this dict with the K8s Kind string and exact `AppsV1Api` method names, then adding/removing kind-specific logic (e.g., HPA is Deployment/StatefulSet only, DaemonSet never has HPA). The intent is that API and rule engine never diverge on what's supported.
- The `api_kind` parameter must always be passed explicitly by the caller — the fetched workload object's `.kind` field is not reliable. This is non-obvious and worth documenting in code if functions ever need modification.
- DaemonSet has no `/scale` subresource and no replica concept — any code reading `replicas` or targeting HPA should skip DaemonSet entirely, as `_hpa_finding()` does.
- The `unsupported_selector_kind` finding ensures a typo in `targets.yaml` is visible in the API rather than crashing the analyzer loop silently. Monitor for this finding in production and correct the ConfigMap.
- SQLite column `cluster_insights.deployments_scanned` was intentionally left unrenamed for backwards-compatibility with existing tables. The code reads it as `workloads_scanned` (Python naming), but the column name is unchanged.
- ReplicaSet selector support remains explicitly out of scope. The project's rules engine was intentionally designed around the three most common workload kinds; ReplicaSet is rarely targeted directly and would add complexity without proportional value.
