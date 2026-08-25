import json
import os

import pandas as pd
import requests
import streamlit as st

from auth import require_login

AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000")
KIND_OPTIONS = ["deployment", "statefulset", "daemonset"]
KIND_LABELS = {"deployment": "Deployment", "statefulset": "StatefulSet", "daemonset": "DaemonSet"}

st.set_page_config(page_title="ai-k8s-eventer - Targets", layout="wide")
require_login()
st.title("Targets")


def api_get(path: str, **params):
    try:
        r = requests.get(f"{AGENT_API_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"agent API unreachable: {e}")
        return None


def api_put(path: str, body: dict):
    try:
        r = requests.put(f"{AGENT_API_URL}{path}", json=body, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"failed to save target: {e}")
        return None


def api_delete(path: str):
    try:
        r = requests.delete(f"{AGENT_API_URL}{path}", timeout=5)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        st.error(f"failed to delete target: {e}")
        return False


def api_post(path: str):
    try:
        r = requests.post(f"{AGENT_API_URL}{path}", timeout=60)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"failed to analyze target: {e}")
        return None


with st.expander("Manage watch targets"):
    manage_targets = api_get("/targets") or []
    target_to_edit = st.selectbox(
        "Edit existing (or leave as New to add one)",
        ["New watch target"] + [t["name"] for t in manage_targets],
    )
    editing = next((t for t in manage_targets if t["name"] == target_to_edit), None)

    name = st.text_input("Name", value=editing["name"] if editing else "", disabled=bool(editing))

    default_kind = editing["selector_kind"] if editing else "deployment"
    selector_kind = st.selectbox(
        "Kind", KIND_OPTIONS, index=KIND_OPTIONS.index(default_kind), format_func=lambda k: KIND_LABELS[k],
    )
    kind_label = KIND_LABELS[selector_kind]

    available_namespaces = api_get("/namespaces") or []
    default_namespace = editing["namespace"] if editing else "default"
    namespace_options = available_namespaces if available_namespaces else [default_namespace]
    if default_namespace not in namespace_options:
        namespace_options = [default_namespace] + namespace_options
    namespace = st.selectbox("Namespace", namespace_options, index=namespace_options.index(default_namespace))

    available_workloads = api_get(f"/namespaces/{namespace}/workloads", kind=selector_kind) or []
    default_workload = editing["selector_name"] if editing else ""
    if available_workloads:
        workload_options = available_workloads
        if default_workload and default_workload not in available_workloads:
            workload_options = [default_workload] + available_workloads
        selector_name = st.selectbox(f"{kind_label} name", workload_options)
    else:
        selector_name = st.text_input(
            f"{kind_label} name", value=default_workload,
            help=f"No {kind_label}s found in '{namespace}' (or it hasn't been created yet) - type its name.",
        )

    instruction = st.text_area(
        "Instruction", value=editing["instruction"] if editing else "",
        help="Plain language, e.g. 'This is the backend, tell me if it's healthy.'",
    )
    col1, col2 = st.columns(2)
    if col1.button("Save") and name and namespace and selector_name:
        api_put(
            f"/targets/{name}",
            {"namespace": namespace, "selector_kind": selector_kind, "selector_name": selector_name, "instruction": instruction},
        )
        st.rerun()
    if col2.button("Delete", disabled=not editing) and editing:
        api_delete(f"/targets/{editing['name']}")
        st.rerun()

targets = api_get("/targets")
if not targets:
    st.warning("No watch targets configured yet. Add one above.")
    st.stop()

target_names = [t["name"] for t in targets]
selected = st.selectbox("Watch target", target_names)


@st.fragment(run_every="15s")
def show_target(name: str):
    target = next((t for t in targets if t["name"] == name), None)
    if target:
        st.caption(f"Instruction: {target['instruction']}")

    if st.button("Analyze now"):
        with st.spinner("Running rules + LLM for this target..."):
            api_post(f"/targets/{name}/analyze")
        st.rerun()

    insight = api_get("/insights/latest", target=name)
    st.subheader("Latest insight")
    if insight:
        status = insight.get("status", "Unknown")
        color = {"Healthy": "green", "Degraded": "orange", "Critical": "red"}.get(status, "gray")
        st.markdown(f"**Status:** :{color}[{status}]")
        st.write("**Issues:**")
        raw_issues = insight.get("issues")
        issues = json.loads(raw_issues) if isinstance(raw_issues, str) else (raw_issues or [])
        for issue in issues:
            st.write(f"- {issue}")
        st.write(f"**Recommendation:** {insight.get('recommendation', '')}")
        st.caption(f"as of {insight.get('created_at', '')}")
    else:
        st.info("No insight generated yet - the analyzer runs on an interval, check back shortly.")

    st.subheader("Rule findings")
    findings = api_get("/findings", target=name)
    if findings:
        st.dataframe(pd.DataFrame(findings)[["check_name", "detail", "created_at"]], use_container_width=True)
        with st.expander("Suggested fixes"):
            # Findings history repeats the same check across ticks - show each
            # check/resource combo's fix once (most recent, since findings
            # already come back newest-first) rather than duplicating it.
            seen = set()
            for f in findings:
                key = (f.get("resource_name"), f["check_name"])
                if key in seen or not f.get("remediation"):
                    continue
                seen.add(key)
                st.markdown(f"**{f['check_name']}** ({f.get('resource_name', '')}) - {f.get('detail', '')}")
                st.markdown(f["remediation"])
    else:
        st.write("No findings recorded.")

    st.subheader("Recent events")
    events = api_get("/events", target=name)
    if events:
        cols = ["reason", "type", "message", "count", "last_seen"]
        st.dataframe(pd.DataFrame(events)[cols], use_container_width=True)
    else:
        st.write("No events recorded yet.")


show_target(selected)
