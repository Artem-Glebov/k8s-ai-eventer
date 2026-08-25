import json
import os
import sys

import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auth import require_login

AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000")

st.set_page_config(page_title="ai-k8s-eventer - Cluster Overview", layout="wide")
require_login()
st.title("Cluster Overview")
st.caption(
    "Creative mode: a periodic, unscoped sweep of every workload (Deployment/StatefulSet/"
    "DaemonSet) in scope - not just watch targets. Adds a periodic LLM call and a full workload "
    "listing on the same CPU-only Ollama instance used elsewhere; off by default "
    "(chart/values.yaml -> creativeMode.enabled)."
)


def api_get(path: str, **params):
    try:
        r = requests.get(f"{AGENT_API_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"agent API unreachable: {e}")
        return None


def api_post(path: str):
    try:
        r = requests.post(f"{AGENT_API_URL}{path}", timeout=180)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"failed to scan: {e}")
        return None


data = api_get("/cluster-insights/latest")
if data is None:
    st.stop()

if not data["enabled"]:
    st.warning(
        "Creative mode is disabled. Enable it in `chart/values.yaml` under `creativeMode.enabled` "
        "if you want it to run automatically on an interval - see the cost note above first. "
        "You can still trigger a one-off scan below regardless."
    )

if st.button("Scan now"):
    with st.spinner("Scanning every workload in scope + running the LLM - this is heavier than a per-target analyze, can take a while..."):
        api_post("/cluster-insights/scan")
    st.rerun()

insight = data["insight"]
st.subheader("Latest cluster insight")
if insight:
    st.markdown(f"**Last scan:** {insight.get('created_at', 'unknown')}")
    status = insight.get("status", "Unknown")
    color = {"Healthy": "green", "Degraded": "orange", "Critical": "red"}.get(status, "gray")
    st.markdown(f"**Status:** :{color}[{status}]")
    st.write("**Issues:**")
    raw_issues = insight.get("issues")
    issues = json.loads(raw_issues) if isinstance(raw_issues, str) else (raw_issues or [])
    for issue in issues:
        st.write(f"- {issue}")
    st.write(f"**Recommendation:** {insight.get('recommendation', '')}")

    raw_namespaces = insight.get("namespaces_scanned")
    namespaces = json.loads(raw_namespaces) if isinstance(raw_namespaces, str) else (raw_namespaces or [])
    duration_s = (insight.get("duration_ms") or 0) / 1000
    st.caption(
        f"Scanned {insight.get('deployments_scanned', 0)} workload(s) across "
        f"{len(namespaces)} namespace(s) ({', '.join(namespaces) or 'none'}) in {duration_s:.1f}s"
    )
else:
    st.info("No cluster scan has run yet - use 'Scan now' above, or enable creativeMode for it to run on an interval.")
