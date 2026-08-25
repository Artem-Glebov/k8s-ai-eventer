# Creative Mode: Periodic Cluster-Wide Deployment Sweep

## What Changed

Previously, the agent only analyzed Deployments that appeared in the user's explicit `watch targets` configuration (the `targets` table). This meant issues in unmonitored Deployments went unseen — the tool required prior knowledge of what to watch. Creative mode closes that gap: a periodic, unscoped sweep that evaluates every Deployment visible to the agent in real time, surfacing problems nobody configured a watch target for.

### Implementation

**Backend (`services/agent/`):**

- `rules.py`: extracted the deterministic health-check logic (`container/pod/HPA/PDB/node-pressure checks`) from `evaluate_target()` into a new shared `_evaluate_deployment(core_v1, autoscaling_v2, policy_v1, rbac_scope, dep)` function that operates on a `V1Deployment` object. `evaluate_target()` is now a thin wrapper: fetch-by-name, then call the shared function. New `evaluate_all_deployments(core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, watch_namespaces)` lists every Deployment in scope using the same scope branching as elsewhere (cluster-wide `list_deployment_for_all_namespaces()` vs. per-namespace `list_namespaced_deployment` over each namespace), runs the shared function against each, and excludes `kube-system`/`kube-public`/`kube-node-lease` from results. Reuses the existing `compute_status()` to aggregate findings into an overall cluster status.

- `llm.py`: new `CLUSTER_SYSTEM_PROMPT` (same "facts-then-narrative" contract as the per-target prompt, adapted for multi-deployment scope), `build_cluster_prompt(status, findings_by_deployment, event_summaries, deployments_scanned, namespaces_scanned)` that caps findings at `CLUSTER_FINDINGS_CAP` (currently 12) and appends a "+N more findings" line rather than silently dropping overflow. New `analyze_cluster(...)` mirrors the per-target `analyze()`: JSON schema `{"issues": [...], "recommendation": "..."}`, same graceful degradation to `"Unknown"` on failure, plus explicit validation that the parsed response contains at least one of `issues`/`recommendation` keys (catches off-schema responses that `.get()` defaults would silently hide).

- `db.py`: new `cluster_insights` table with columns `status`, `issues`, `recommendation`, `raw`, `deployments_scanned`, `namespaces_scanned`, `duration_ms`, `created_at`. New `insert_cluster_insight(...)` and `latest_cluster_insight()` methods. `prune()` now also prunes this table on the existing retention schedule.

- `main.py`: new env vars `CREATIVE_MODE_ENABLED` (default `false`), `CREATIVE_MODE_INTERVAL_SECONDS` (default `600`), and `CLUSTER_NUM_PREDICT` (default `500`). New `run_creative_mode_tick(...)` function times itself, calls `evaluate_all_deployments()`, computes status, retrieves cluster-wide event summaries via the existing `db.recent_events_summary_all()`, calls `analyze_cluster()`, and stores results. Background thread `creative_mode_loop()` runs only when `CREATIVE_MODE_ENABLED=true`; zero cost when disabled. Manual on-demand trigger via `app.state.run_creative_mode_tick`.

- `api.py`: new `GET /cluster-insights/latest` returning `{"enabled": bool, "insight": {...} | null}` and `POST /cluster-insights/scan` for manual triggering (works regardless of the enabled flag).

- `chart/values.yaml` / `chart/templates/agent-deployment.yaml`: new `creativeMode: {enabled: false, intervalSeconds: 600}` block with explicit cost note. New `ollama.llm.clusterNumPredict: 500` setting.

**Frontend (`services/ui/`):**

- `pages/2_Cluster_Overview.py` (new file): Streamlit auto-discovers it from the `pages/` directory. If creative mode is disabled, shows a toggle note pointing at `values.yaml`. Always shows the latest `cluster_insights` row if it exists (status color-mapped same as per-target), a caption with actual counts (`"Scanned 8 deployment(s) across 2 namespace(s) ... in 88.4s"`), and a "Scan now" button.

## Why This Design, and What Was Rejected

**Full unscoped sweep, not a rollup of existing watch targets:**
Rolling up only the targets someone already configured would duplicate Phase 3's chat feature (chat reads each target's latest insight). Creative mode's entire purpose is discovering unknown unknowns — Deployments nobody thought to watch.

**Off by default, longer interval, cost stated and visible:**
This is materially more expensive than the per-target analyzer: every tick lists all Deployments (extra API server load), makes one more periodic LLM call on an already-constrained Ollama instance shared with per-target analysis and chat (no request queuing between them). The default interval (600s) is deliberately twice the per-target analyzer's (180s). The UI shows actual `deployments_scanned`/`namespaces_scanned`/`duration_ms` from each run — cost isn't a comment, it's observable.

**Dedicated `cluster_insights` table, not a sentinel row in `insights`:**
The per-target `/insights/latest` endpoint requires a real `targets` table row (404 otherwise). A fake sentinel target would need special cases everywhere. A separate table keeps cluster summary a genuinely distinct feed.

**Excluding kube-system/kube-public/kube-node-lease:**
Discovered in testing: including system namespaces added noise (Deployments nobody cares about) and, combined with a longer fact list, pushed the 3B model into hallucinating off-schema responses. Nobody sets Kubernetes internals as personal watch targets anyway.

**Reusing `_evaluate_deployment()` via extraction:**
Ensures a Deployment gets identical findings whether analyzed as a named watch target or picked up by the unscoped sweep — no drift risk between the two code paths.

**Dedicated `CLUSTER_NUM_PREDICT=500` vs. `LLM_NUM_PREDICT=300`:**
Discovered in testing: the per-target budget (300 tokens) was insufficient for a multi-deployment fact list; the model produced truncated JSON. The cluster sweep uses a separate, higher budget (500) without affecting per-target analysis.

## Verification

- `python3 -m py_compile` on all changed/new agent files. Re-ran the HPA/PDB severity check verification to confirm the `evaluate_target()` refactor didn't regress existing per-target analysis.
- `helm template ./chart` with `creativeMode.enabled=true` and `=false` confirmed new env vars render correctly.
- Deployed with default (`creativeMode.enabled: false`); confirmed `GET /cluster-insights/latest` returns `{"enabled": false, "insight": null}` with no background thread running.
- Manual `POST /cluster-insights/scan` against live cluster surfaced two real bugs that were fixed in the same session:
  1. **LLM response truncated mid-JSON** (deployments_scanned: 10, included kube-system noise). Root cause: `LLM_NUM_PREDICT=300` insufficient for longer fact list. Fixed with dedicated `CLUSTER_NUM_PREDICT=500`.
  2. **LLM produced off-schema JSON** (looked like Kubernetes event object, not `{"issues": [...], "recommendation": "..."}`) that `.get()` defaults silently accepted as "no issues found." Fixed three ways: excluded system namespaces (reduced noise), lowered `CLUSTER_FINDINGS_CAP` to 12, repeated exact required schema at end of prompt before generation, and added validation in `analyze_cluster()` that explicitly raises (and degrades to "Unknown") if `issues` and `recommendation` keys are both missing.
  3. **Third attempt after all three fixes:** correct schema, `deployments_scanned: 8`, kube-system correctly excluded, status `Critical` citing real finding (`ImagePullBackOff` in test fixture's `bad-image-app`), no errors in logs.

The new `pages/2_Cluster_Overview.py` page needs manual browser verification (no browser automation available in this environment).

## What to Know for Maintenance

- Cluster-wide summarization is qualitatively harder for a 3B model than per-target analysis (multiple unrelated Deployments/namespaces at once). The fixes above are reliable at current scale (single-digit to low-double-digit Deployment count). Larger clusters may require tuning `CLUSTER_FINDINGS_CAP`/`CLUSTER_NUM_PREDICT` again or coarser aggregation (e.g., counting findings by check type instead of listing each individually).
- Both `creativeMode` and the per-target analyzer call the same Ollama instance with no queuing between them — if both tick around the same time, one simply waits for the other. This was an accepted tradeoff, not engineered around.
- The response-schema validation in `analyze_cluster()` (checking for `issues`/`recommendation` keys) does not exist in the per-target `analyze()`. The per-target case hasn't produced off-schema responses, likely because its fact lists are shorter and more homogeneous. Worth adding the same guard there if ever observed.

## Bug Fix: Cluster Overview Findings Omission (2026-08-20)

### Problem

The Cluster Overview issues list and LLM recommendation could silently omit equally-or-more-critical problems on different workloads. For example: a noisy Deployment with many same-severity findings (e.g., a 2-pod workload hitting `oom_killed`, `high_restart_count`, and `container_waiting` — 6 Critical findings total) could occupy every slot in the top-N findings list, completely hiding a second, unrelated Critical workload (e.g., a `bad-image-app` with a single Critical `ImagePullBackOff`) from both the deterministic issues list and the LLM's fact block.

### Root Cause

Two places ranked cluster-wide findings by pure severity alone: `llm._rank_cluster_findings()` (called by both `llm.top_cluster_issues()` for the deterministic issues list and `llm.build_cluster_prompt()` for the LLM's FACTS block). A flat severity sort allowed one noisy workload's multiple findings to fill every slot in the capped result before any other workload's findings were considered. Additionally, no deduplication occurred at the source: multiple occurrences of the same check firing on different pods within one workload were treated as separate findings, multiplying noise.

### Fix

Two complementary changes:

1. **Deduplication in `rules.py`**: new `_dedupe_findings()` helper, called only from `evaluate_all_workloads()` (per-target `evaluate_target()` is unaffected). Within one workload's finding list, multiple occurrences of the same `check_name` (e.g., the same check firing on 2 pods) are merged into a single finding. The first occurrence's `detail` and `remediation` are preserved, with "(+N more occurrence(s) of the same check on other pods/containers)" appended to the detail. This cuts noise and frees capacity in downstream capped lists for distinct problems across different workloads.

2. **Coverage-first ranking in `llm.py`**: `_rank_cluster_findings()` rewritten from a flat severity sort to a round-robin algorithm. Workloads are visited in order of their worst finding's severity (Critical-affected workloads first), and one finding is taken from each workload per round, cycling until all findings across all workloads are ranked. This guarantees every distinct problem workload gets at least one slot before any workload gets a second — as long as `CLUSTER_FINDINGS_CAP` (currently 12) or `top_cluster_issues()`'s `n` (currently 3) exceeds the number of distinct affected workloads, none can be silently dropped. Both `top_cluster_issues()` and `build_cluster_prompt()` call this shared function, ensuring the deterministic list and LLM input never diverge.

### Verification

A standalone Python script (stubbing `kubernetes` and `ollama` imports) simulated the reported scenario: a 2-pod `oom-app` producing 6 raw Critical findings, plus a `bad-image-app` with 1 Critical finding. Confirmed: `_dedupe_findings()` collapsed `oom-app`'s 6 findings to 3 (one per distinct check, each annotated with occurrences); `top_cluster_issues(n=3)` now includes `bad-image-app` in its result (previously entirely absent, crowded out by `oom-app`'s findings).

**Confirmed live, same day** (2026-08-20, after the `ai-eventer` minikube was brought back up): `make deploy` rebuilt/reloaded the agent image, and `exec`-grepping the running pod's `/app/rules.py`/`/app/llm.py` confirmed the new code was actually running (not a stale image). A `POST /cluster-insights/scan` against the live cluster (10 workloads across `ai-eventer`/`test-app`, which happened to already contain the exact reported scenario — `bad-image-app` with one Critical `ImagePullBackOff`, plus `oom-app` and `crash-loop-app` each with multiple same-check findings across pods) returned all three distinct problem workloads in the top-3 issues list — `bad-image-app` no longer crowded out. The dedup was visible directly in the output text: `"container stress has restarted 132 times (+1 more occurrence(s) of the same check on other pods/containers)"` for `oom-app`'s two pods. Status `Critical`, correct schema, no parse errors, `duration_ms: 85691`.

