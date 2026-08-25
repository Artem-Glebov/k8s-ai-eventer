import logging
import os
import threading
import time

import bcrypt
import uvicorn
import yaml
from kubernetes import client, config
from ollama import Client as OllamaClient

import db
import llm
import notify
import rules
import watch
from api import app
from util import to_sqlite_ts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

DB_PATH = os.environ.get("DB_PATH", "/data/eventer.db")
TARGETS_SEED_FILE = os.environ.get("TARGETS_SEED_FILE", "/config/targets.yaml")
RBAC_SCOPE = os.environ.get("RBAC_SCOPE", "cluster")
WATCH_NAMESPACES = [ns for ns in os.environ.get("WATCH_NAMESPACES", "").split(",") if ns]
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
LLM_NUM_PREDICT = int(os.environ.get("LLM_NUM_PREDICT", "300"))
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "2048"))
CHAT_NUM_PREDICT = int(os.environ.get("CHAT_NUM_PREDICT", "400"))
# Cluster-wide sweeps can carry far more FACTS (up to CLUSTER_FINDINGS_CAP)
# than a single target's analyze() ever does - LLM_NUM_PREDICT=300 was
# observed truncating the JSON response mid-object on a 10-deployment sweep.
CLUSTER_NUM_PREDICT = int(os.environ.get("CLUSTER_NUM_PREDICT", "500"))
ANALYZE_INTERVAL_SECONDS = int(os.environ.get("ANALYZE_INTERVAL_SECONDS", "180"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
CREATIVE_MODE_ENABLED = os.environ.get("CREATIVE_MODE_ENABLED", "false").lower() == "true"
CREATIVE_MODE_INTERVAL_SECONDS = int(os.environ.get("CREATIVE_MODE_INTERVAL_SECONDS", "600"))
ADMIN_BOOTSTRAP_USERNAME = os.environ.get("ADMIN_BOOTSTRAP_USERNAME", "admin")
ADMIN_BOOTSTRAP_EMAIL = os.environ.get("ADMIN_BOOTSTRAP_EMAIL", "admin@example.com")
ADMIN_BOOTSTRAP_PASSWORD = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"


def seed_targets():
    if not os.path.exists(TARGETS_SEED_FILE):
        return
    with open(TARGETS_SEED_FILE) as f:
        targets = yaml.safe_load(f) or []
    for t in targets:
        selector = t.get("selector", {})
        db.upsert_target(
            name=t["name"],
            namespace=t["namespace"],
            selector_kind=selector.get("kind", "deployment"),
            selector_name=selector.get("name", t["name"]),
            instruction=t.get("instruction", ""),
        )
    logger.info("seeded %d watch target(s) from %s", len(targets), TARGETS_SEED_FILE)


def seed_admin():
    if db.count_users() > 0:
        return
    if not ADMIN_BOOTSTRAP_PASSWORD:
        logger.warning(
            "no ADMIN_BOOTSTRAP_PASSWORD set and users table is empty - "
            "no admin account created, UI login has nothing to authenticate against"
        )
        return
    password_hash = bcrypt.hashpw(ADMIN_BOOTSTRAP_PASSWORD.encode(), bcrypt.gensalt()).decode()
    db.upsert_user_password(ADMIN_BOOTSTRAP_USERNAME, "Admin", ADMIN_BOOTSTRAP_EMAIL, password_hash)
    logger.info("seeded initial admin user %s", ADMIN_BOOTSTRAP_USERNAME)


def on_event(evt):
    involved = evt.involved_object
    ts = evt.last_timestamp or evt.event_time or evt.metadata.creation_timestamp
    db.upsert_event(
        uid=evt.metadata.uid,
        namespace=evt.metadata.namespace,
        involved_kind=involved.kind if involved else None,
        involved_name=involved.name if involved else None,
        reason=evt.reason,
        message=evt.message,
        type_=evt.type,
        count=evt.count or 1,
        first_seen=to_sqlite_ts(evt.first_timestamp or ts),
        last_seen=to_sqlite_ts(ts),
    )


def analyze_one_target(core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, ollama_client, t) -> dict:
    result = rules.evaluate_target(
        core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope,
        t["namespace"], t["selector_kind"], t["selector_name"],
    )
    findings = result["findings"]
    if findings:
        db.insert_findings(t["name"], t["namespace"], findings)
    db.set_target_resources(t["name"], t["namespace"], [t["selector_name"], *result["pod_names"]])
    summaries = db.recent_event_summaries(t["name"])
    status = rules.compute_status(findings)
    remediation = rules.pick_primary_remediation(findings)
    _maybe_notify_critical(t, status, findings, remediation)
    # issues are deterministic (top findings' own detail text, most severe
    # first) - the LLM is only asked for "recommendation" now, since asking
    # it to also summarize "issues" was observed dropping concrete
    # identifiers (image name, resource name) in favor of vague phrasing.
    issues = [f["detail"] for f in rules.top_findings(findings)]
    llm_result = llm.analyze(
        ollama_client, OLLAMA_MODEL, status, t["instruction"], findings, summaries, remediation,
        LLM_TEMPERATURE, LLM_NUM_PREDICT, LLM_NUM_CTX,
    )
    db.insert_insight(t["name"], status, issues, llm_result["recommendation"], llm_result["raw"])
    return {"status": status, "issues": issues, "recommendation": llm_result["recommendation"], "raw": llm_result["raw"]}


def _maybe_notify_critical(t: dict, status: str, findings: list[dict], remediation: str | None) -> None:
    # Debounced on a state transition, not a fixed timer: fires once when a
    # target goes Degraded/Healthy -> Critical, stays silent while it remains
    # Critical tick after tick, and fires again if it flaps
    # Critical -> recovered -> Critical. No stored state yet is treated as an
    # implicit "Healthy" baseline, so a target that's already Critical on its
    # very first analysis still notifies immediately - same "never delay a
    # real problem" behavior this project already has everywhere else.
    state = db.get_notification_state(t["name"])
    previous_status = state["last_status"] if state else "Healthy"
    transitioned = status == "Critical" and previous_status != "Critical"
    if not transitioned:
        db.set_notification_state(t["name"], status, notified=False)
        return
    sent = False
    if SMTP_HOST:
        sent = notify.send_critical_alert(
            SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM, SMTP_USE_TLS,
            db.notification_recipients(), t["name"], t["namespace"], t["selector_kind"],
            t["selector_name"], rules.top_findings(findings), remediation,
        )
    if sent:
        db.set_notification_state(t["name"], status, notified=True)
    # else: leave notification_state untouched (or unset) so the next tick
    # still sees previous_status != "Critical" and retries the send, instead
    # of silently giving up on a real, still-unreported Critical transition.


def build_target_insight_summaries() -> list[dict]:
    summaries = []
    for t in db.rows_to_dicts(db.list_targets()):
        insight = db.latest_insight(t["name"])
        summaries.append({
            "name": t["name"],
            "namespace": t["namespace"],
            "selector_name": t["selector_name"],
            "status": insight["status"] if insight else "Unknown",
            "recommendation": insight["recommendation"] if insight else "not yet analyzed",
        })
    return summaries


def stream_chat_response(ollama_client, message: str, history: list[dict]):
    context = llm.build_chat_context(build_target_insight_summaries(), db.recent_events_summary_all())
    yield from llm.stream_chat(
        ollama_client, OLLAMA_MODEL, context, history, message,
        LLM_TEMPERATURE, CHAT_NUM_PREDICT, LLM_NUM_CTX,
    )


def run_creative_mode_tick(core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, watch_namespaces, ollama_client) -> dict:
    start = time.monotonic()
    sweep = rules.evaluate_all_workloads(
        core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, watch_namespaces,
    )
    all_findings = [f for findings in sweep["findings_by_workload"].values() for f in findings]
    status = rules.compute_status(all_findings)
    remediation = rules.pick_primary_remediation(all_findings)
    # Same deterministic-issues rationale as analyze_one_target(): the LLM is
    # only asked for "recommendation" now.
    issues = llm.top_cluster_issues(sweep["findings_by_workload"])
    summaries = db.recent_events_summary_all()
    llm_result = llm.analyze_cluster(
        ollama_client, OLLAMA_MODEL, status, sweep["findings_by_workload"], summaries,
        sweep["workloads_scanned"], sweep["namespaces_scanned"], remediation,
        LLM_TEMPERATURE, CLUSTER_NUM_PREDICT, LLM_NUM_CTX,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    db.insert_cluster_insight(
        status, issues, llm_result["recommendation"], llm_result["raw"],
        sweep["workloads_scanned"], sweep["namespaces_scanned"], duration_ms,
    )
    return {"status": status, "issues": issues, "recommendation": llm_result["recommendation"],
            "raw": llm_result["raw"], "workloads_scanned": sweep["workloads_scanned"],
            "namespaces_scanned": sweep["namespaces_scanned"], "duration_ms": duration_ms}


def creative_mode_loop(core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, watch_namespaces, ollama_client, stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            result = run_creative_mode_tick(
                core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, watch_namespaces, ollama_client,
            )
            logger.info(
                "creative mode tick: status=%s workloads=%d duration_ms=%d",
                result["status"], result["workloads_scanned"], result["duration_ms"],
            )
        except Exception:
            logger.exception("creative mode tick failed")
        stop_event.wait(CREATIVE_MODE_INTERVAL_SECONDS)


def analyzer_loop(core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, ollama_client, stop_event: threading.Event):
    while not stop_event.is_set():
        for t in db.list_targets():
            try:
                result = analyze_one_target(core_v1, apps_v1, autoscaling_v2, policy_v1, rbac_scope, ollama_client, t)
                logger.info("analyzed target=%s status=%s", t["name"], result["status"])
            except Exception:
                logger.exception("analyzer tick failed for target=%s", t["name"])
        db.prune(RETENTION_DAYS)
        stop_event.wait(ANALYZE_INTERVAL_SECONDS)


def main():
    db.init_db(DB_PATH)
    seed_targets()
    seed_admin()

    config.load_incluster_config()
    core_v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    autoscaling_v2 = client.AutoscalingV2Api()
    policy_v1 = client.PolicyV1Api()
    ollama_client = OllamaClient(host=OLLAMA_HOST)

    # Shared with api.py so the read layer can list live namespaces/deployments
    # for the UI's dropdowns and trigger an on-demand re-analyze - same process,
    # same clients, no separate credentials for the API layer.
    app.state.core_v1 = core_v1
    app.state.apps_v1 = apps_v1
    app.state.ollama_client = ollama_client
    app.state.rbac_scope = RBAC_SCOPE
    app.state.watch_namespaces = WATCH_NAMESPACES
    app.state.analyze_one_target = lambda t: analyze_one_target(
        core_v1, apps_v1, autoscaling_v2, policy_v1, RBAC_SCOPE, ollama_client, t,
    )
    app.state.stream_chat = lambda message, history: stream_chat_response(ollama_client, message, history)
    app.state.creative_mode_enabled = CREATIVE_MODE_ENABLED
    app.state.run_creative_mode_tick = lambda: run_creative_mode_tick(
        core_v1, apps_v1, autoscaling_v2, policy_v1, RBAC_SCOPE, WATCH_NAMESPACES, ollama_client,
    )

    stop_event = threading.Event()
    watch.start(core_v1, RBAC_SCOPE, WATCH_NAMESPACES, on_event, stop_event)

    analyzer_thread = threading.Thread(
        target=analyzer_loop,
        args=(core_v1, apps_v1, autoscaling_v2, policy_v1, RBAC_SCOPE, ollama_client, stop_event),
        daemon=True,
    )
    analyzer_thread.start()

    if CREATIVE_MODE_ENABLED:
        creative_mode_thread = threading.Thread(
            target=creative_mode_loop,
            args=(core_v1, apps_v1, autoscaling_v2, policy_v1, RBAC_SCOPE, WATCH_NAMESPACES, ollama_client, stop_event),
            daemon=True,
        )
        creative_mode_thread.start()

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
