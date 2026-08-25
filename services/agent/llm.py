"""Prompt assembly and Ollama calls. Facts-then-narrative design: deterministic
rule findings and pre-aggregated event counts are handed to the model as a
short fact list it's told never to contradict, with a fixed output template
and a capped token budget — small CPU-only models drift/hallucinate quickly
on open-ended prompts, much less so on rigid, fact-anchored ones."""

import json
import logging

from ollama import Client

from rules import SEVERITY_BY_CHECK, STATUS_RANK

logger = logging.getLogger("llm")

SYSTEM_PROMPT = """You are a Kubernetes reliability assistant.
You are given a STATUS (already determined by deterministic rules - do not question or \
restate a different one), a fixed list of FACTS (deterministic rule findings and aggregated \
recent event counts - already shown to the user separately, do not restate them), a SUGGESTED \
FIX (already computed for the most severe fact), and a user INSTRUCTION describing what to pay \
attention to.
Only reference the facts given below - never invent counts, resource names, or numbers that are \
not listed. RECENT EVENTS is background context only - never derive a recommendation from it, \
only from FACTS and the SUGGESTED FIX.
The "recommendation" must be a natural-language paraphrase of the SUGGESTED FIX, never an \
alternative you invent yourself - the fix has already been correctly worked out for you.
Respond ONLY with JSON matching exactly this schema, no extra text:
{"recommendation": "one sentence"}"""


CHAT_SYSTEM_PROMPT = """You are a Kubernetes reliability assistant chatting with an operator.
You are given a CONTEXT block below - it IS the current cluster state (an "Overall" rollup, \
each watch target's latest stored status/recommendation, and recent cluster events). It is not \
a partial excerpt: if asked for a general overview of cluster/pod health, answer directly from \
the Overall line and the per-target list - that request is always covered, never say the \
information isn't available for it.
Only decline to answer, citing missing information, when asked about something genuinely absent \
from CONTEXT (e.g. a target/resource name that isn't listed, or metrics never mentioned).
Never invent counts, resource names, or numbers that aren't in CONTEXT.
Always reply in the same language the operator's question was written in, and do not mix \
languages within a reply."""


def build_chat_context(target_insights: list[dict], event_summaries: list[str]) -> str:
    lines = ["CONTEXT:"]
    if target_insights:
        counts: dict[str, int] = {}
        for t in target_insights:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        rollup = ", ".join(f"{n} {status}" for status, n in counts.items())
        lines.append(f"Overall: {len(target_insights)} watch target(s) - {rollup}.")
    else:
        lines.append("Overall: no watch targets configured.")
    lines.append("")
    lines.append("Watch target status:")
    if target_insights:
        lines.extend(
            f"- {t['name']} ({t['namespace']}/{t['selector_name']}): "
            f"status={t['status']}, recommendation={t['recommendation']}"
            for t in target_insights
        )
    else:
        lines.append("- no watch targets configured")
    lines.append("")
    lines.append("Recent cluster events:")
    if event_summaries:
        lines.extend(f"- {s}" for s in event_summaries)
    else:
        lines.append("- no recent events")
    return "\n".join(lines)


def stream_chat(
    ollama_client: Client,
    model: str,
    context: str,
    history: list[dict],
    user_message: str,
    temperature: float,
    num_predict: int,
    num_ctx: int,
):
    messages = [{"role": "system", "content": f"{CHAT_SYSTEM_PROMPT}\n\n{context}"}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    try:
        stream = ollama_client.chat(
            model=model,
            messages=messages,
            stream=True,
            options={"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx},
        )
        for chunk in stream:
            content = chunk.message.content
            if content:
                yield content
    except Exception as e:
        # Same graceful-degradation convention as analyze(): a broken stream
        # should show up as an in-band assistant message, not a dead connection.
        logger.warning("chat stream failed: %s", e)
        yield f"\n\n_(error talking to the model: {e})_"


def build_prompt(
    status: str, instruction: str, findings: list[dict], event_summaries: list[str],
    remediation: str | None,
) -> str:
    lines = [f"STATUS: {status}", "", "FACTS:"]
    if findings:
        lines.extend(f"- {f['check_name']}: {f.get('detail', '')}" for f in findings)
    else:
        lines.append("- no rule violations detected")
    lines.append("")
    lines.append("RECENT EVENTS (aggregated, background context only - not a source for issues):")
    if event_summaries:
        lines.extend(f"- {s}" for s in event_summaries)
    else:
        lines.append("- no recent events")
    lines.append("")
    lines.append(f"SUGGESTED FIX: {remediation or 'none - no issues detected'}")
    lines.append("")
    lines.append(f"INSTRUCTION: {instruction}")
    return "\n".join(lines)


def analyze(
    ollama_client: Client,
    model: str,
    status: str,
    instruction: str,
    findings: list[dict],
    event_summaries: list[str],
    remediation: str | None,
    temperature: float,
    num_predict: int,
    num_ctx: int,
) -> dict:
    prompt = build_prompt(status, instruction, findings, event_summaries, remediation)
    try:
        response = ollama_client.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format="json",
            options={"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx},
        )
        content = response.message.content
        parsed = json.loads(content)
        return {"recommendation": parsed.get("recommendation", ""), "raw": content}
    except Exception as e:
        # Model may not be pulled yet (see ollama-pull-job), Ollama may be
        # cold-starting, or the 3B model may return malformed JSON. Degrade
        # gracefully - the next analyzer tick tries again. status/issues are
        # computed deterministically by the caller regardless of this call's
        # outcome, so only the narrative recommendation is affected here.
        logger.warning("LLM call failed or returned unparseable output: %s", e)
        return {"recommendation": "", "raw": str(e)}


CLUSTER_SYSTEM_PROMPT = """You are a Kubernetes reliability assistant.
You are given a STATUS (already determined by deterministic rules across every workload \
(Deployment/StatefulSet/DaemonSet) in the cluster - do not question or restate a different one), \
a fixed list of FACTS each labeled with the kind/namespace/name it came from, and a SUGGESTED FIX \
(already computed for the most severe fact). This is a broad, unscoped sweep - not one specific \
watch target - so multiple unrelated workloads may appear together in FACTS.
Only reference the facts given below - never invent counts, resource names, or numbers that are \
not listed. RECENT EVENTS is background context only - never derive a recommendation from it, \
only from FACTS and the SUGGESTED FIX.
The "recommendation" must be a natural-language paraphrase of the SUGGESTED FIX, never an \
alternative you invent yourself - the fix has already been correctly worked out for you.
Respond ONLY with JSON matching exactly this schema, no extra text:
{"recommendation": "one sentence"}"""

CLUSTER_FINDINGS_CAP = 12


def _rank_cluster_findings(findings_by_workload: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    """Coverage-first, then severity: round-robin one finding at a time across
    workloads (workloads visited in order of their own worst finding's
    severity) rather than a flat severity sort across every finding. A flat
    sort let one noisy workload's several same-severity findings crowd out
    every other workload's problem once CLUSTER_FINDINGS_CAP/top_cluster_issues'
    `n` truncated the list - observed directly: a workload with 6 Critical
    findings (multiple pods x multiple checks) hid a second, unrelated
    Critical workload's single finding entirely. Round-robin guarantees every
    problem workload gets at least one slot before any workload gets a
    second, as long as the cap covers the number of distinct workloads.
    Shared by build_cluster_prompt() (FACTS ordering/capping) and
    top_cluster_issues() (the deterministic "issues" list) so the two ranking
    passes never diverge."""
    def severity_of(f):
        return STATUS_RANK.index(SEVERITY_BY_CHECK.get(f["check_name"], "Degraded"))

    per_workload = {
        key: sorted(findings, key=severity_of, reverse=True)
        for key, findings in findings_by_workload.items()
        if findings
    }
    workload_order = sorted(per_workload, key=lambda key: severity_of(per_workload[key][0]), reverse=True)

    flat = []
    round_idx = 0
    while any(round_idx < len(per_workload[key]) for key in workload_order):
        for key in workload_order:
            if round_idx < len(per_workload[key]):
                flat.append((key, per_workload[key][round_idx]))
        round_idx += 1
    return flat


def top_cluster_issues(findings_by_workload: dict[str, list[dict]], n: int = 3) -> list[str]:
    """Deterministic "issues" list for the cluster-wide sweep - same
    rationale as rules.top_findings(): the LLM was observed producing vague
    bullets ("Image does not exist", "ImagePullSecrets not set") that dropped
    the concrete resource/image identifiers already present in each
    finding's own `detail` text, so it's never asked to summarize this."""
    return [f"{key}: {f['detail']}" for key, f in _rank_cluster_findings(findings_by_workload)[:n]]


def build_cluster_prompt(
    status: str,
    findings_by_workload: dict[str, list[dict]],
    event_summaries: list[str],
    workloads_scanned: int,
    namespaces_scanned: list[str],
    remediation: str | None,
) -> str:
    flat = _rank_cluster_findings(findings_by_workload)
    shown = flat[:CLUSTER_FINDINGS_CAP]

    lines = [
        f"STATUS: {status}",
        "",
        f"Scanned {workloads_scanned} workload(s) across namespace(s): "
        f"{', '.join(namespaces_scanned) or 'none'}.",
        "",
        "FACTS:",
    ]
    if shown:
        lines.extend(f"- {key}: {f['check_name']} - {f.get('detail', '')}" for key, f in shown)
    else:
        lines.append("- no rule violations detected across any scanned workload")
    remaining = len(flat) - len(shown)
    if remaining > 0:
        remaining_workloads = len({key for key, _ in flat[len(shown):]})
        lines.append(f"- (+{remaining} more finding(s) not shown, across {remaining_workloads} other workload(s))")
    lines.append("")
    lines.append("RECENT EVENTS (aggregated, cluster-wide, background context only - not a source for issues):")
    if event_summaries:
        lines.extend(f"- {s}" for s in event_summaries)
    else:
        lines.append("- no recent events")
    lines.append("")
    lines.append(f"SUGGESTED FIX: {remediation or 'none - no issues detected'}")
    lines.append("")
    # Repeated here, not just in the system prompt - small models attend more
    # reliably to a schema instruction placed right before generation than
    # one stated earlier and further away (observed: a longer FACTS list here
    # was enough to make the model emit some other, unrelated JSON shape).
    lines.append('Respond with ONLY this JSON shape, no other keys, no extra text: '
                 '{"recommendation": "one sentence"}')
    return "\n".join(lines)


def analyze_cluster(
    ollama_client: Client,
    model: str,
    status: str,
    findings_by_workload: dict[str, list[dict]],
    event_summaries: list[str],
    workloads_scanned: int,
    namespaces_scanned: list[str],
    remediation: str | None,
    temperature: float,
    num_predict: int,
    num_ctx: int,
) -> dict:
    prompt = build_cluster_prompt(
        status, findings_by_workload, event_summaries, workloads_scanned, namespaces_scanned,
        remediation,
    )
    try:
        response = ollama_client.chat(
            model=model,
            messages=[
                {"role": "system", "content": CLUSTER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format="json",
            options={"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx},
        )
        content = response.message.content
        parsed = json.loads(content)
        # A cluster-wide sweep hands the model far more/more varied FACTS
        # than a single-target analyze() ever does - observed directly: valid
        # JSON that didn't match the expected key at all (the model invented
        # an unrelated shape). Treat that as a failure, not an empty
        # recommendation, or a confused response silently looks fine.
        if "recommendation" not in parsed:
            raise ValueError(f"response didn't match the expected schema: {content[:200]}")
        return {"recommendation": parsed.get("recommendation", ""), "raw": content}
    except Exception as e:
        logger.warning("cluster LLM call failed or returned unparseable output: %s", e)
        return {"recommendation": "", "raw": str(e)}
