# HPA, PDB, and Node Pressure Checks

## What Changed

`services/agent/rules.py`'s `evaluate_target()` gained three new deterministic checks following the existing fact-list pattern (findings are fed to the LLM prompt in `llm.py` as immutable facts):

1. **`missing_hpa`** — new helper `_hpa_finding()` lists `HorizontalPodAutoscaler`s in the namespace via `autoscaling_v2.list_namespaced_horizontal_pod_autoscaler`, flags if none has `spec.scale_target_ref` pointing at the target Deployment.
2. **`missing_pdb`** — new helper `_pdb_finding()` lists `PodDisruptionBudget`s via `policy_v1.list_namespaced_pod_disruption_budget`, flags if none has a `spec.selector.match_labels` that is a subset of the Deployment's pod-selector labels.
3. **`node_pressure`** — new helper `_node_pressure_findings()` reads `core_v1.read_node()` for each live pod's node, checks `node.status.conditions` for `MemoryPressure`/`DiskPressure`/`PIDPressure` == `"True"`, and deduplicates per `(node_name, condition_type)` pair to keep the fact list compact for the 3B model's token budget.

### Implementation

- `evaluate_target()` signature changed: now accepts `autoscaling_v2`, `policy_v1`, `rbac_scope` as new parameters (positioned after `apps_v1`, before `namespace`).
- `services/agent/main.py` instantiates `client.AutoscalingV2Api()` and `client.PolicyV1Api()`, threads them (and the existing `RBAC_SCOPE` constant) through `analyze_one_target()` and `analyzer_loop()`.
- **Node-pressure check scope gating:** only runs when `rbac_scope == "cluster"`. `nodes` is a cluster-scoped resource — a per-namespace `RoleBinding` cannot grant access to it, only a `ClusterRoleBinding` can. This mirrors the pre-existing pattern in `services/agent/api.py`'s `GET /namespaces` endpoint. HPA and PDB checks run under both scope modes (they are namespaced resources).
- `chart/templates/rbac.yaml`'s `ClusterRole` gained two new rules:
  ```yaml
  - apiGroups: ["autoscaling"]
    resources: ["horizontalpodautoscalers"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["policy"]
    resources: ["poddisruptionbudgets"]
    verbs: ["get", "list", "watch"]
  ```
- No changes to `services/agent/db.py` (findings.check_name is free-text), `services/agent/llm.py` (new findings flow through existing fact-list prompt unchanged), or `chart/values.yaml`.
- RBAC change went through Plan Mode per this project's rules before being merged.

## Why This Design, and What Was Rejected

**Scope gating vs. error swallowing:**
Node-pressure gating on `rbac_scope` (rather than wrapping `read_node` in a try/except swallowing 403 Forbidden) matches the project's explicit style in `api.py` — consistent scope checks and fallbacks beat marginally simpler implicit error handling.

**Per-node-and-condition dedup (not per-pod):**
Repeating the same "node is under pressure" fact once per pod sharing that node would bloat the prompt with no informational gain. This project's LLM (CPU-only, `num_ctx=2048`) is documented as needing a compact fact list to avoid hallucination — dedup is a deliberate token-budget decision.

**Helper functions over inline logic:**
HPA/PDB checks return a single finding-or-None from small helpers, matching the existing `_uses_latest_tag`-style pattern already in `rules.py`, rather than inlining directly into `evaluate_target`.

## Verification

- `helm template ./chart --set rbac.scope=cluster` and `--set rbac.scope=namespaces --set watchNamespaces='{default}'` confirmed both new `ClusterRole` rules render correctly under both scope modes.
- `python3 -m py_compile` on `rules.py` and `main.py` confirmed no syntax errors.
- Deployed via `make deploy` to `ai-eventer` minikube; no errors in agent logs after rollout.
- Triggered `POST /targets/{name}/analyze` against a test watch target (`Test-app`) pointed at an intentionally-misconfigured `oom-app` Deployment with no HPA or PDB. Confirmed via `GET /findings?target=Test-app` that `missing_hpa` and `missing_pdb` findings were produced correctly.
- **Node-pressure logic:** verified by code inspection only. Single-node minikube has no way to force a real `MemoryPressure`/`DiskPressure` condition; confirmed no errors reading node status in agent logs, but the "flags correctly when a node is under pressure" path was not exercised end-to-end. Known verification gap.

## Update 2026-08-18: status severity moved out of the LLM's hands

Once a real user tried this against `test-app`'s `healthy-app` (a Deployment with proper
resources/probes/pinned image, but no HPA/PDB), the LLM-decided `status` field came back
`"Critical"` - the model treated "2 issues present" as worst-case regardless of what those issues
actually were. This defeated the point of `missing_hpa`/`missing_pdb` being lower-stakes
best-practice gaps rather than active failures.

Fixed by removing status classification from the LLM's job entirely, extending the same
"deterministic facts, no model judgment calls" philosophy this project already applies elsewhere.
`rules.py` now has `SEVERITY_BY_CHECK` (a per-check-name severity: `container_waiting`,
`oom_killed`, `high_restart_count`, `node_pressure`, and `target_not_found` are `Critical`
- they represent something actively broken right now; `missing_requests`/`missing_limits`/
`missing_liveness_probe`/`missing_readiness_probe`/`latest_image_tag`/`missing_hpa`/`missing_pdb`
are `Degraded` - resilience/best-practice gaps on an otherwise-working workload) and
`compute_status(findings)` (returns `"Healthy"` for no findings, otherwise the highest severity
present). `main.py`'s `analyze_one_target()` now calls this before invoking the LLM and passes the
result into `llm.analyze()` as a new `status` parameter. `llm.py`'s `SYSTEM_PROMPT`/`build_prompt()`
now present `STATUS` to the model as an already-decided fact it must not contradict, and the
expected JSON response schema dropped `"status"` entirely - the model only produces `issues`/
`recommendation` now. `analyze()` returns the deterministically-computed status regardless of
anything the model outputs (there's nothing for it to output there anymore). On an LLM-call
failure the status still falls back to `"Unknown"`, not the computed one - keeps the existing
"no narrative was possible this tick" signal separate from a real severity value.

This also fixes chat overview quality "for free": `/chat`'s context is built from each target's
*stored* `insights.status`, so once analyzer ticks re-run with this fix, chat summaries inherit
correct severities with no separate change needed.

Verified: forced re-analysis of `test-heathy` (→ `healthy-app`, only `missing_hpa`/`missing_pdb`)
now returns `"Degraded"`; `Test-app` (→ `oom-app`, real `container_waiting`/`oom_killed`/
`high_restart_count`) still correctly returns `"Critical"`.

## What to Know for Maintenance

- When `rbac.scope: namespaces` is used, node-pressure advice will silently never appear — this is intentional least-privilege behavior, not a bug, but worth knowing if someone wonders why it never flags.
- The PDB coverage check treats a PDB as covering the Deployment if its `matchLabels` are a subset of the Deployment's `matchLabels`. It does not handle PDBs with `matchExpressions` instead (rare in practice but a real gap) — such a PDB would be invisible and the Deployment would incorrectly show `missing_pdb`.
- With more facts now available per target (6+ in the test case with overlapping pods), the LLM sometimes conflates unrelated facts — e.g. suggesting an HPA would limit restarts. This is a pre-existing small-model prompting limitation, not specific to this change, but worth noting as a candidate for future prompt-tuning now that the fact list is longer.
- **Update 2026-08-19**: `oom_killed`'s "Critical" severity assumes it represents something actively broken *right now* — but `container_status.last_state.terminated` never clears on its own, it holds the most recent termination indefinitely until the next restart. Without a recency check, a single OOM from hours/days ago on an otherwise-stable container showed "Critical" forever. Fixed with `rules.OOM_RECENCY_MINUTES` (20 min, matching the existing event-recency window in `db.recent_event_summaries`) — the finding only fires if `finished_at` is within that window. Also added the pod name into `oom_killed`'s `detail`/`remediation` (previously the only finding without one), since its absence was observed causing the LLM to borrow an unrelated pod name from RECENT EVENTS instead (e.g. misattributing a real Ollama Deployment OOM to an unrelated one-shot `ollama-pull` Job pod that merely appeared nearby in event context). Verified live: the same stale Ollama OOM (~56 min old) stopped appearing in a fresh cluster sweep, and the reported status correctly shifted to a genuinely current issue instead. `high_restart_count` has the same theoretical staleness gap (a cumulative counter that never resets except on pod recreation) but wasn't changed here — flagged to the user as a candidate for the same treatment, not yet decided.
- **Update 2026-08-20**: `high_restart_count`'s staleness gap is now fixed. Like `oom_killed`, `container_status.restart_count` is cumulative and never resets except on pod recreation — a container that restarted 5+ times long ago and has since stabilized would show "Critical" forever without a recency check. Fixed by adding `rules.RESTART_RECENCY_MINUTES` (20 min, same window and reasoning as `OOM_RECENCY_MINUTES`, kept as a separate constant to allow independent tuning later). The finding now reads `cs.last_state.terminated.finished_at` and only fires if that finish time is within the recency window, following the same "missing finished_at treated as not-recent" convention as `oom_killed`. Also added the pod name into `high_restart_count`'s `detail` (previously missing, like `oom_killed` before its fix), so the detail now reads like `"container busybox in pod crash-loop-app-784ffdd9bc-fq5gc has restarted 137 times, most recently within the last 20 minutes"`. Verified with a standalone script testing hand-built fake `V1ContainerStatus` objects (no cluster needed) — a restart 2 minutes ago at count 6 still fires, a restart 25 minutes ago at count 6 does not, a restart 1 minute ago below the threshold does not. Then verified live against `ai-eventer` after `make deploy`, with a `POST /cluster-insights/scan` against the `test-app` namespace's fixtures — the continuously-restarting pods correctly reported `high_restart_count` findings with the pod name and recency phrase now included.
