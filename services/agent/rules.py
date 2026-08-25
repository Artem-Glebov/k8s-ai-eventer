"""Deterministic checks that produce structured facts for the LLM prompt.
These exist so the model never has to invent whether limits/probes are
missing or restarts are high — it only narrates over facts computed here."""

from datetime import datetime, timedelta, timezone

from kubernetes.client.exceptions import ApiException

WAITING_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"}
RESTART_THRESHOLD = 5
# last_state.terminated never clears on its own - it holds the *most recent*
# termination indefinitely, until the next restart overwrites it. Without a
# recency window, a single OOM from hours/days ago would show "Critical"
# forever on an otherwise-stable container. 20 minutes matches the existing
# "recent" window used for event context (db.recent_event_summaries) - past
# it, the container has demonstrably stabilized and this stops being current.
OOM_RECENCY_MINUTES = 20
# restart_count has the identical "never resets on its own" problem as OOMKilled
# above - it's cumulative and only clears on pod recreation, so a pod that
# restarted 5+ times long ago and has been stable since would otherwise show
# "Critical" forever. Same window/reasoning as OOM_RECENCY_MINUTES, kept as a
# separate constant so the two can be tuned independently later if needed.
RESTART_RECENCY_MINUTES = 20
NODE_PRESSURE_CONDITIONS = {"MemoryPressure", "DiskPressure", "PIDPressure"}
# Kubernetes-internal namespaces aren't user workloads and nobody sets them as
# a watch target - including them in a cluster-wide sweep is pure noise (and,
# observed directly, enough noise on a small model to derail the whole
# response). Only applies under rbac.scope=cluster; an explicit
# watchNamespaces list is trusted as-is.
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}

# Single source of truth for which workload kinds a watch target/cluster sweep
# can evaluate, and which AppsV1Api method names to call for each - shared
# with api.py so the two never drift on supported selector_kind values.
# V1DeploymentSpec/V1StatefulSetSpec/V1DaemonSetSpec all expose identical
# .selector/.template.spec.containers shapes (verified against the installed
# kubernetes client), so one evaluation helper serves all three.
KIND_INFO = {
    "deployment": {
        "api_kind": "Deployment",
        "read": "read_namespaced_deployment",
        "list_namespaced": "list_namespaced_deployment",
        "list_all": "list_deployment_for_all_namespaces",
    },
    "statefulset": {
        "api_kind": "StatefulSet",
        "read": "read_namespaced_stateful_set",
        "list_namespaced": "list_namespaced_stateful_set",
        "list_all": "list_stateful_set_for_all_namespaces",
    },
    "daemonset": {
        "api_kind": "DaemonSet",
        "read": "read_namespaced_daemon_set",
        "list_namespaced": "list_namespaced_daemon_set",
        "list_all": "list_daemon_set_for_all_namespaces",
    },
}
SUPPORTED_SELECTOR_KINDS = set(KIND_INFO)

# Overall target status is computed here, deterministically, rather than left
# to the LLM to judge from a bulleted fact list - a small model asked to grade
# severity tends to treat "any issues at all" as worst-case (observed: a
# healthy, running Deployment marked "Critical" solely for missing an HPA/PDB).
# Checks that mean something is actively broken right now outrank checks that
# are best-practice/resilience gaps on an otherwise-working workload.
STATUS_RANK = ["Healthy", "Degraded", "Critical"]
SEVERITY_BY_CHECK = {
    "target_not_found": "Critical",
    "container_waiting": "Critical",
    "oom_killed": "Critical",
    "high_restart_count": "Critical",
    "node_pressure": "Critical",
    "missing_requests": "Degraded",
    "missing_limits": "Degraded",
    "missing_liveness_probe": "Degraded",
    "missing_readiness_probe": "Degraded",
    "latest_image_tag": "Degraded",
    "missing_hpa": "Degraded",
    "missing_pdb": "Degraded",
    # Can't evaluate the target at all - same severity as target_not_found.
    "unsupported_selector_kind": "Critical",
}


def _severity_rank(finding: dict) -> int:
    # Unknown check names default to "Degraded" (safe middle ground) rather
    # than silently being treated as Healthy.
    return STATUS_RANK.index(SEVERITY_BY_CHECK.get(finding["check_name"], "Degraded"))


def compute_status(findings: list[dict]) -> str:
    if not findings:
        return "Healthy"
    return STATUS_RANK[max(_severity_rank(f) for f in findings)]


def pick_primary_remediation(findings: list[dict]) -> str | None:
    """The LLM is only ever asked to paraphrase this, never invent its own fix
    - same "most severe first" ranking compute_status() uses, so the
    remediation shown always matches the finding driving the overall status."""
    if not findings:
        return None
    return max(findings, key=_severity_rank).get("remediation")


def top_findings(findings: list[dict], n: int = 3) -> list[dict]:
    """Most severe first, capped at n - the deterministic source for a
    user-facing "issues" list. The LLM is never asked to summarize/select
    which issues matter: observed producing vague bullets ("Image does not
    exist", "ImagePullSecrets not set") that dropped concrete identifiers
    (which image, which resource) already present in each finding's own
    `detail` text. Same rationale as pick_primary_remediation()."""
    return sorted(findings, key=_severity_rank, reverse=True)[:n]


def _uses_latest_tag(image: str) -> bool:
    """True if `image` isn't pinned to an immutable, non-latest reference:
    no tag at all (Kubernetes defaults an untagged image to :latest), or an
    explicit `:latest` tag. A digest reference (`@sha256:...`) is pinned
    regardless of any tag alongside it, so it's never flagged."""
    if "@" in image:
        return False
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return True
    return last_segment.rsplit(":", 1)[-1] == "latest"


def _container_findings(containers, resource_name: str) -> list[dict]:
    findings = []
    for c in containers:
        if _uses_latest_tag(c.image):
            findings.append({
                "resource_name": resource_name,
                "check_name": "latest_image_tag",
                "detail": f"container {c.name} uses image '{c.image}' without a pinned, non-latest tag",
                "remediation": (
                    f"Pin container `{c.name}`'s image to an explicit version or digest instead "
                    f"of `{c.image}`, e.g. `{c.image.rsplit(':', 1)[0]}:<explicit-version>` or "
                    f"`{c.image.rsplit(':', 1)[0]}@sha256:<digest>`."
                ),
            })
        resources = c.resources
        requests = (resources.requests or {}) if resources else {}
        limits = (resources.limits or {}) if resources else {}
        if not requests.get("cpu") or not requests.get("memory"):
            findings.append({
                "resource_name": resource_name,
                "check_name": "missing_requests",
                "detail": f"container {c.name} is missing cpu/memory requests",
                "remediation": (
                    f"Add resource requests to container `{c.name}` (adjust to what it actually "
                    f"needs):\n```yaml\nresources:\n  requests:\n    cpu: 100m\n    memory: 128Mi\n```"
                ),
            })
        if not limits.get("cpu") or not limits.get("memory"):
            findings.append({
                "resource_name": resource_name,
                "check_name": "missing_limits",
                "detail": f"container {c.name} is missing cpu/memory limits",
                "remediation": (
                    f"Add resource limits to container `{c.name}` (adjust to what it actually "
                    f"needs):\n```yaml\nresources:\n  limits:\n    cpu: 500m\n    memory: 256Mi\n```"
                ),
            })
        if not c.liveness_probe:
            findings.append({
                "resource_name": resource_name,
                "check_name": "missing_liveness_probe",
                "detail": f"container {c.name} has no liveness probe",
                "remediation": (
                    f"Add a liveness probe to container `{c.name}` (use whatever health "
                    f"endpoint/port it actually serves):\n```yaml\nlivenessProbe:\n  httpGet:\n"
                    f"    path: /healthz\n    port: <container-port>\n  initialDelaySeconds: 10\n"
                    f"  periodSeconds: 10\n```"
                ),
            })
        if not c.readiness_probe:
            findings.append({
                "resource_name": resource_name,
                "check_name": "missing_readiness_probe",
                "detail": f"container {c.name} has no readiness probe",
                "remediation": (
                    f"Add a readiness probe to container `{c.name}` (use whatever health "
                    f"endpoint/port it actually serves):\n```yaml\nreadinessProbe:\n  httpGet:\n"
                    f"    path: /healthz\n    port: <container-port>\n  initialDelaySeconds: 5\n"
                    f"  periodSeconds: 10\n```"
                ),
            })
    return findings


def _pod_findings(pod, resource_name: str) -> list[dict]:
    findings = []
    pod_name = pod.metadata.name
    for cs in pod.status.container_statuses or []:
        if cs.restart_count and cs.restart_count >= RESTART_THRESHOLD:
            last_terminated = cs.last_state.terminated if cs.last_state else None
            # Only a recently-occurring restart counts as "actively broken right
            # now" (this check's Critical severity assumes that) - same
            # missing-finished_at-treated-as-not-recent convention as oom_killed.
            restart_is_recent = bool(
                last_terminated and last_terminated.finished_at and
                datetime.now(timezone.utc) - last_terminated.finished_at <= timedelta(minutes=RESTART_RECENCY_MINUTES)
            )
            if restart_is_recent:
                findings.append({
                    "resource_name": resource_name,
                    "check_name": "high_restart_count",
                    "detail": (
                        f"container {cs.name} in pod {pod_name} has restarted {cs.restart_count} "
                        f"times, most recently within the last {RESTART_RECENCY_MINUTES} minutes"
                    ),
                    "remediation": (
                        f"A restart count alone doesn't say why - run `kubectl describe pod {pod_name}` "
                        f"to see the real root cause for `{cs.name}`."
                    ),
                })
        state = cs.state
        waiting = state.waiting if state else None
        if waiting and waiting.reason in WAITING_REASONS:
            findings.append({
                "resource_name": resource_name,
                "check_name": "container_waiting",
                "detail": f"container {cs.name} is waiting: {waiting.reason} ({waiting.message or 'no message'})",
                "remediation": (
                    f"Verify the image `{cs.image}` exists, the tag/digest is correct, and it's "
                    f"reachable from the cluster (registry auth, imagePullSecrets)."
                    if waiting.reason in ("ImagePullBackOff", "ErrImagePull") else
                    f"Run `kubectl logs {pod_name} -c {cs.name} --previous` to see why `{cs.name}` "
                    f"is exiting, and check its command/config/image - not a scaling or "
                    f"backoff-timing setting, which doesn't exist."
                ),
            })
        # A container that OOMKilled and is now back in CrashLoopBackOff no
        # longer has a terminated `state` - Kubernetes moves that info to
        # `last_state` once it starts waiting to restart. Check both, or the
        # OOM fact silently disappears the moment the pod starts backing off.
        terminated = state.terminated if state else None
        last_terminated = cs.last_state.terminated if cs.last_state else None
        oom_terminated = next(
            (t for t in (terminated, last_terminated) if t and t.reason == "OOMKilled"),
            None,
        )
        # Only current/recent OOMs count as "actively broken right now" (this
        # check's Critical severity assumes that) - finished_at is always
        # populated on a real termination, so a missing one is treated as
        # not-recent rather than guessed at.
        oom_is_recent = bool(
            oom_terminated and oom_terminated.finished_at and
            datetime.now(timezone.utc) - oom_terminated.finished_at <= timedelta(minutes=OOM_RECENCY_MINUTES)
        )
        if oom_is_recent:
            findings.append({
                "resource_name": resource_name,
                "check_name": "oom_killed",
                "detail": (
                    f"container {cs.name} in pod {pod_name} was OOMKilled within the last "
                    f"{OOM_RECENCY_MINUTES} minutes (exit code {oom_terminated.exit_code})"
                ),
                "remediation": (
                    f"Raise container `{cs.name}`'s memory limit, or reduce its memory usage - it "
                    f"was OOMKilled (pod `{pod_name}`), not simply restarted, so more replicas or a "
                    f"longer backoff won't help."
                ),
            })
    return findings


def _hpa_finding(autoscaling_v2, namespace: str, workload_kind: str, workload_name: str, resource_name: str) -> dict | None:
    # DaemonSet has no /scale subresource (no replica count concept at all -
    # confirmed via the client having no *_daemon_set_scale methods, present
    # for Deployment/StatefulSet) - an HPA can never target one, so don't
    # even ask, or this would always false-positive "missing_hpa" on every
    # DaemonSet.
    if workload_kind == "DaemonSet":
        return None
    hpas = autoscaling_v2.list_namespaced_horizontal_pod_autoscaler(namespace).items
    for hpa in hpas:
        ref = hpa.spec.scale_target_ref
        if ref.kind == workload_kind and ref.name == workload_name:
            return None
    return {
        "resource_name": resource_name,
        "check_name": "missing_hpa",
        "detail": f"no HorizontalPodAutoscaler targets {workload_kind.lower()} {workload_name}",
        "remediation": (
            f"Add a HorizontalPodAutoscaler:\n```yaml\napiVersion: autoscaling/v2\n"
            f"kind: HorizontalPodAutoscaler\nmetadata:\n  name: {workload_name}\nspec:\n"
            f"  scaleTargetRef:\n    apiVersion: apps/v1\n    kind: {workload_kind}\n"
            f"    name: {workload_name}\n  minReplicas: 1\n  maxReplicas: 5\n  metrics:\n"
            f"    - type: Resource\n      resource:\n        name: cpu\n        target:\n"
            f"          type: Utilization\n          averageUtilization: 80\n```"
        ),
    }


def _pdb_finding(policy_v1, namespace: str, match_labels: dict, resource_name: str) -> dict | None:
    pdbs = policy_v1.list_namespaced_pod_disruption_budget(namespace).items
    for pdb in pdbs:
        selector = pdb.spec.selector
        pdb_labels = (selector.match_labels or {}) if selector else {}
        if pdb_labels and all(match_labels.get(k) == v for k, v in pdb_labels.items()):
            return None
    labels_yaml = "\n".join(f"      {k}: {v}" for k, v in match_labels.items()) or "      {}"
    return {
        "resource_name": resource_name,
        "check_name": "missing_pdb",
        "detail": f"no PodDisruptionBudget covers this workload's pods (resource: {resource_name})",
        "remediation": (
            f"Add a PodDisruptionBudget:\n```yaml\napiVersion: policy/v1\n"
            f"kind: PodDisruptionBudget\nmetadata:\n  name: {resource_name}\nspec:\n"
            f"  minAvailable: 1\n  selector:\n    matchLabels:\n{labels_yaml}\n```"
        ),
    }


def _node_pressure_findings(core_v1, pods, resource_name: str) -> list[dict]:
    findings = []
    reported = set()
    node_cache = {}
    for pod in pods:
        node_name = pod.spec.node_name
        if not node_name:
            continue
        if node_name not in node_cache:
            try:
                node_cache[node_name] = core_v1.read_node(node_name)
            except ApiException:
                node_cache[node_name] = None
        node = node_cache[node_name]
        if node is None:
            continue
        for cond in node.status.conditions or []:
            key = (node_name, cond.type)
            if cond.type in NODE_PRESSURE_CONDITIONS and cond.status == "True" and key not in reported:
                reported.add(key)
                findings.append({
                    "resource_name": resource_name,
                    "check_name": "node_pressure",
                    "detail": f"node {node_name} (running this target's pods) reports {cond.type}",
                    "remediation": (
                        f"This is a node-level condition, not something fixable by editing this "
                        f"workload - run `kubectl describe node {node_name}` to check its "
                        f"capacity/pressure, or move this workload to a less-pressured node."
                    ),
                })
    return findings


def _evaluate_workload(core_v1, autoscaling_v2, policy_v1, rbac_scope: str, api_kind: str, workload) -> dict:
    """Runs every check against an already-fetched V1Deployment/V1StatefulSet/
    V1DaemonSet. All three expose identical .metadata/.spec.selector/
    .spec.template.spec.containers shapes, so one function serves all three
    kinds. Shared by evaluate_target() (one named workload) and
    evaluate_all_workloads() (every workload in scope, for creative mode) so
    the two never drift.

    api_kind is the literal Kind string ("Deployment"/"StatefulSet"/
    "DaemonSet") passed by the caller - not read off workload.kind, which is
    frequently empty on single-object GET responses (only reliably populated
    on LIST) and would silently break the HPA scaleTargetRef.kind match.

    Returns {"findings": [...], "pod_names": [...]} — pod_names is the live
    set of pods owned by the workload right now, resolved via its label
    selector. Callers reuse this list to widen event matching beyond the
    workload's own name (see db.set_target_resources) instead of doing a
    second, separate pod lookup."""
    namespace = workload.metadata.namespace
    name = workload.metadata.name

    findings = _container_findings(workload.spec.template.spec.containers, name)

    hpa_finding = _hpa_finding(autoscaling_v2, namespace, api_kind, name, name)
    if hpa_finding:
        findings.append(hpa_finding)

    match_labels = workload.spec.selector.match_labels or {}
    pdb_finding = _pdb_finding(policy_v1, namespace, match_labels, name)
    if pdb_finding:
        findings.append(pdb_finding)

    label_selector = ",".join(f"{k}={v}" for k, v in match_labels.items())
    pods = core_v1.list_namespaced_pod(namespace, label_selector=label_selector).items
    for pod in pods:
        findings.extend(_pod_findings(pod, name))

    # nodes are cluster-scoped: a per-namespace RoleBinding can never grant
    # access to them, only a ClusterRoleBinding can (same reason api.py's
    # /namespaces falls back under scope=namespaces).
    if rbac_scope == "cluster":
        findings.extend(_node_pressure_findings(core_v1, pods, name))

    return {"findings": findings, "pod_names": [pod.metadata.name for pod in pods]}


def evaluate_target(
    core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope: str,
    namespace: str, selector_kind: str, workload_name: str,
) -> dict:
    """Watch targets select a workload (Deployment/StatefulSet/DaemonSet) by
    name. Other selector kinds (labels, ReplicaSet) remain a later expansion."""
    info = KIND_INFO.get(selector_kind)
    if info is None:
        # Defensive: seed_targets() reads selector.kind from the targets.yaml
        # ConfigMap with no validation (only api.py's PUT /targets validates),
        # so a typo/stale DB row must degrade to a visible finding, not crash
        # the analyzer loop with a KeyError on every tick.
        return {
            "findings": [{
                "resource_name": workload_name,
                "check_name": "unsupported_selector_kind",
                "detail": f"selector_kind '{selector_kind}' is not one of {sorted(SUPPORTED_SELECTOR_KINDS)}",
                "remediation": f"Edit this watch target and pick a supported kind: {sorted(SUPPORTED_SELECTOR_KINDS)}.",
            }],
            "pod_names": [],
        }
    try:
        workload = getattr(apps_v1, info["read"])(workload_name, namespace)
    except ApiException as e:
        if e.status == 404:
            return {
                "findings": [{
                    "resource_name": workload_name,
                    "check_name": "target_not_found",
                    "detail": f"{info['api_kind'].lower()} {workload_name} not found in namespace {namespace}",
                    "remediation": (
                        f"Check the watch target's namespace/name/kind is correct, or that "
                        f"`{workload_name}` in `{namespace}` hasn't been deleted/renamed."
                    ),
                }],
                "pod_names": [],
            }
        raise
    return _evaluate_workload(core_v1, autoscaling_v2, policy_v1, rbac_scope, info["api_kind"], workload)


def _dedupe_findings(findings: list[dict]) -> list[dict]:
    """Multiple pods/containers under one workload can trigger the same
    check_name repeatedly (e.g. two pods both OOMKilled) - each is a genuine
    grounded fact, but stacking near-duplicates crowds a cluster-wide sweep's
    capped FACTS list with repeats of one workload's problem instead of
    surfacing other workloads' distinct problems. Merge same-check
    occurrences into one, keeping the first (still grounded, real-example)
    detail/remediation and noting how many more there were."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for f in findings:
        check = f["check_name"]
        if check not in merged:
            merged[check] = dict(f)
            order.append(check)
        else:
            merged[check]["_extra"] = merged[check].get("_extra", 0) + 1
    result = []
    for check in order:
        f = merged[check]
        extra = f.pop("_extra", 0)
        if extra:
            f["detail"] = f"{f['detail']} (+{extra} more occurrence(s) of the same check on other pods/containers)"
        result.append(f)
    return result


def evaluate_all_workloads(
    core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope: str, watch_namespaces: list[str],
) -> dict:
    """Creative mode: every Deployment/StatefulSet/DaemonSet the agent can
    see, not just ones with a configured watch target - the closest thing to
    "what am I not watching that I should be." Reuses the exact same checks
    as evaluate_target() via _evaluate_workload(), just run over a listing
    instead of one lookup.

    Returns {"findings_by_workload": {"Kind:ns/name": [...], ...},
    "namespaces_scanned": [...], "workloads_scanned": N}. Keys are prefixed
    with the Kind because a Deployment and a DaemonSet can legitimately share
    a name in the same namespace."""
    findings_by_workload = {}
    namespaces_seen = set()
    workloads_scanned = 0

    for info in KIND_INFO.values():
        if rbac_scope == "cluster":
            workloads = [
                w for w in getattr(apps_v1, info["list_all"])().items
                if w.metadata.namespace not in SYSTEM_NAMESPACES
            ]
        else:
            workloads = [
                w for ns in watch_namespaces for w in getattr(apps_v1, info["list_namespaced"])(ns).items
            ]
        workloads_scanned += len(workloads)
        for w in workloads:
            namespaces_seen.add(w.metadata.namespace)
            key = f"{info['api_kind']}:{w.metadata.namespace}/{w.metadata.name}"
            findings_by_workload[key] = _dedupe_findings(_evaluate_workload(
                core_v1, autoscaling_v2, policy_v1, rbac_scope, info["api_kind"], w,
            )["findings"])

    namespaces_scanned = sorted(namespaces_seen) if rbac_scope == "cluster" else sorted(watch_namespaces)
    return {
        "findings_by_workload": findings_by_workload,
        "namespaces_scanned": namespaces_scanned,
        "workloads_scanned": workloads_scanned,
    }
