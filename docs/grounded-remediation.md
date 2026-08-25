# Grounded Remediation

## What Changed

The LLM's `"recommendation"` field in analysis output was repeatedly inventing plausible-sounding but incorrect fixes — fake settings like "restart limit" for `CrashLoopBackOff`, "scale the deployment to reduce backoff time", and once even a completely fabricated JSON response shape. Earlier patches had tried to fix this by adding one more "X isn't a real thing" bullet to the system prompt per bug found, which was unsustainable. The real fix extends the project's established "facts-then-narrative" design to the remediation itself: concrete, actionable fixes are now computed inline from real values available at the point each finding is discovered, persisted to the database, and surfaced to the LLM as a `SUGGESTED FIX` section that grounds its `"recommendation"` field.

### Implementation

- **`services/agent/rules.py`:** every finding dict (across `_container_findings`, `_pod_findings`, `_hpa_finding`, `_pdb_finding`, `_node_pressure_findings`, and the `target_not_found` early-return) now carries a `"remediation"` key with concrete guidance computed inline using real values in scope:
  - `missing_requests`/`missing_limits`: a ready YAML `resources.requests`/`limits` block naming the real container with placeholder sensible values (100m/128Mi for requests, 500m/256Mi for limits)
  - `missing_liveness_probe`/`missing_readiness_probe`: template `httpGet` probe block with a caveat that path/port must match what the container actually serves
  - `latest_image_tag`: the real current image string and exact pinning syntax
  - `container_waiting`: branches on the real `waiting.reason` — for `ImagePullBackOff`/`ErrImagePull` verifies the real image string; for `CrashLoopBackOff` gives exact `kubectl logs <pod> -c <container> --previous` command with an explicit note this is a command/config/image problem, not a backoff-timing setting
  - `oom_killed`: names the real container, notes that memory must be raised or usage reduced; more replicas/backoff won't help
  - `high_restart_count`: exact `kubectl describe pod <pod>` command with the note that restart count alone doesn't explain cause
  - `missing_hpa`: complete, ready HPA manifest with `scaleTargetRef` naming the real Deployment
  - `missing_pdb`: complete, ready PDB manifest using the Deployment's real `matchLabels`
  - `node_pressure`: explicitly states this is node-level and not fixable by editing the Deployment; gives `kubectl describe node <name>` command with the real node name
  - `target_not_found`: advises checking the watch target's namespace/name is correct, naming the real (missing) name/namespace
- New `rules.pick_primary_remediation(findings) -> str | None`: selects the single most severe finding using the same `SEVERITY_BY_CHECK`/`STATUS_RANK` ranking `compute_status()` uses, returns that finding's `remediation` text (or `None` for empty findings)
- **`services/agent/db.py`:** `findings` table gains a nullable `remediation TEXT` column; `init_db()` runs `ALTER TABLE findings ADD COLUMN remediation TEXT` wrapped in try/except to silently no-op on existing tables (first schema migration beyond `CREATE TABLE IF NOT EXISTS`); `insert_findings()` updated to write the new column
- **`services/agent/llm.py`:** `build_prompt()` and `build_cluster_prompt()` now accept a `remediation: str | None` parameter and append `SUGGESTED FIX: <text>` section after FACTS/EVENTS (or `"none - no issues detected"` when there isn't one); `analyze()` and `analyze_cluster()` thread this parameter through; `SYSTEM_PROMPT` and `CLUSTER_SYSTEM_PROMPT` simplified by replacing the accumulated "X isn't a real setting" bullets with one instruction: when a SUGGESTED FIX is given, the `"recommendation"` must paraphrase it, never invent an alternative; both prompts also gained explicit note that RECENT EVENTS is background context only and must never source an issue or fix
- **`services/agent/main.py`:** `analyze_one_target()` calls `rules.pick_primary_remediation(findings)` and passes it to `llm.analyze()`; `run_creative_mode_tick()` does the same for cluster-wide findings
- **`services/ui/Targets.py`** (renamed from `services/ui/app.py` — see separate note below): "Rule findings" section gained a "Suggested fixes" expander below the raw findings dataframe with fixes deduplicated by `(resource_name, check_name)` and rendered via `st.markdown()` for proper Markdown/YAML syntax highlighting instead of literal newlines in dataframe cells
- **`services/ui/pages/2_Cluster_Overview.py`:** added prominent `**Last scan:** <timestamp>` line above status instead of only a small trailing mention in the cost caption

## Why This Design, and What Was Rejected

**Inline remediation at finding-creation time, not a static lookup table:** An earlier idea was a `check_name -> recommendation` dictionary. This was rejected because real values available at each finding site (container name, current image, Deployment name, real `match_labels`, real pod names) make the guidance directly actionable — a copy-pasteable manifest or runnable `kubectl` command — rather than a generic sentence.

**Single "SUGGESTED FIX" for the most severe finding, not one per FACT:** Embedding full remediation text (some multi-line YAML) into every FACT line would blow the token budget, especially in cluster-wide sweeps (already capped at 12 findings). Since `compute_status()` already ranks findings to pick a single worst severity, using that same ranking to pick one representative fix keeps the recommendation coherent with the status.

**Persisted on the finding row, not only passed to the LLM:** Even with grounding, a 3B model's paraphrase could occasionally be clumsy or drop a detail. The raw, always-correct source of truth needed its own visible surface in the database and UI regardless of LLM prose quality.

**Expander with `st.markdown`, not a dataframe column:** Discovered during implementation — dataframe cells containing fenced YAML blocks showed literal `\n` and backticks, defeating copy-paste usability.

**Simplifying system prompts rather than keeping old examples alongside the new instruction:** The old "X isn't real" bullets were superseded and removed to avoid prompt growth by one special case per bug found (the exact pattern the user objected to).

## Verification

- `python3 -m py_compile` on all changed files
- Regression check: `POST /targets/test-heathy/analyze` (→ `Degraded` status, only `missing_hpa`/`missing_pdb`) and `POST /targets/Test-app/analyze` (→ `Critical` status, real `CrashLoopBackOff`/`OOMKilled`) still worked correctly; adding `remediation` to every finding dict didn't disturb `compute_status()` or finding shape
- `GET /findings?target=Test-app` confirmed real, correct `remediation` text per finding — `missing_hpa` carried a complete manifest naming the real Deployment, `container_waiting` (CrashLoopBackOff) had the exact `kubectl logs` command with real pod/container names, `oom_killed` said to raise the real container's memory limit
- LLM `"recommendation"` field now correctly paraphrases the suggested fix instead of inventing one:
  - Per-target: `test-heathy` recommendation was a direct paraphrase of HPA/PDB suggested fix with no invented mechanism
  - Cluster-wide: `POST /cluster-insights/scan` on `test-app`/`ai-eventer` deployments returned `Critical` correctly citing the real `bad-image-app` `ImagePullBackOff` finding with `issues`/`recommendation` both grounded in the real image string and matching the suggested fix — no fabricated JSON shape, no scaling/backoff invention; scan completed in 39 seconds
- UI "Suggested fixes" expander and renamed `Targets.py` nav entry not independently verified in a browser (no browser automation tooling available in this environment)

## What to Know for Maintenance

- Every check in `SEVERITY_BY_CHECK` now has matching `remediation` computed at its finding site. New checks added to `rules.py` must include concrete remediation text at the same time — use real values in scope, prefer runnable commands or ready manifests over vague sentences — otherwise `pick_primary_remediation()` may return `None` for that check even when it's most severe, and the LLM prompt falls back to "none - no issues detected" while a real issue exists
- HPA/PDB suggested YAML manifests use fixed placeholders (`minReplicas: 1, maxReplicas: 5`, `averageUtilization: 80` for HPA; `minAvailable: 1` for PDB) — reasonable generic starting points, not workload-specific
- `pick_primary_remediation()`'s tie-breaking follows Python's `max()` semantics: first occurrence wins. Deterministic given fixed findings append order in `_evaluate_workload()` (renamed from `_evaluate_deployment()`, see update below), but don't rely on it if that order changes
- This change is scoped to Deployment only; ReplicaSet/StatefulSet selector support is a separate "expand selector kind" item in the project's master plan
- **Update 2026-08-19**: StatefulSet and DaemonSet selector support was added (see `docs/statefulset-daemonset-support.md`) — `rules.py`'s `KIND_INFO` dispatch table is now the source of truth for supported kinds, and `_evaluate_deployment()`/`evaluate_all_deployments()` were renamed to `_evaluate_workload()`/`evaluate_all_workloads()`. ReplicaSet selector support remains out of scope.
- **Update 2026-08-19 — same fix extended to `"issues"`**: the user flagged a Cluster Overview result whose "Issues" bullets were uninformative ("Image does not exist", "ImagePullSecrets not set" — no image name, no resource name). Root cause: unlike `"recommendation"` (a paraphrase of a pre-computed `SUGGESTED FIX`), `"issues"` was still something the LLM composed itself by summarizing FACTS — and it was dropping the concrete identifiers (image string, resource/pod name) already present in each finding's own `detail` text in favor of generic phrasing. Fixed the same way remediation was: stopped asking the model for `"issues"` at all. New `rules.top_findings(findings, n=3)` (most severe first, sharing the same `_severity_rank()` helper `compute_status()`/`pick_primary_remediation()` now use) and `llm.top_cluster_issues(findings_by_workload, n=3)` (the cluster-wide equivalent, keyed `"Kind:ns/name: detail"`) compute `"issues"` directly from real finding data in `main.py`; the model's JSON schema shrank to `{"recommendation": "one sentence"}` for both `analyze()` and `analyze_cluster()`. Also fixed a related inconsistency found while doing this: on an LLM call failure, `status`/`issues` used to be overwritten with `"Unknown"`/`[]` even though both are fully computable independent of the LLM — now only `"recommendation"` degrades to `""` on failure, `status`/`issues` always reflect the real deterministic findings. Verified live: the same cluster sweep that previously produced the vague bullets now returns e.g. `"Deployment:test-app/bad-image-app: container nginx is waiting: ImagePullBackOff (Back-off pulling image \"nginx:this-tag-does-not-exist\": ...)"` — concrete and specific, with `"recommendation"` still a correct natural-language paraphrase of the SUGGESTED FIX.
