# Ad-Hoc Chat Interface

## What Changed

Added a new Streamlit page (`services/ui/pages/1_Chat.py`) offering cluster-wide freeform chat, separate from the existing per-watch-target analysis. Streamlit auto-discovers any `.py` file in a `pages/` directory as a sidebar-navigable page — no Dockerfile changes needed.

### Backend additions (all in `services/agent/`)

**Database:**
- `db.py`: new `recent_events_summary_all(minutes=20, limit=30)` function — same output shape as the existing per-target `recent_event_summaries()` but queries the `events` table directly with no `target_resources` join, returning cluster-wide events without target-specific filtering.

**LLM and API:**
- `llm.py`: 
  - `CHAT_SYSTEM_PROMPT` — instructs the model to answer using a context block and decline to guess about anything not covered.
  - `build_chat_context(target_insights, event_summaries)` — assembles a text block with one line per watch target showing its latest stored status and recommendation, plus recent cluster-wide events.
  - `stream_chat(ollama_client, model, context, history, user_message, temperature, num_predict, num_ctx)` — generator wrapping `ollama_client.chat(..., stream=True)`, yielding each chunk's content as it arrives. On failure, logs a warning and yields an in-band error string instead of raising, matching `analyze()`'s existing degradation behavior.
- `main.py`:
  - New env var `CHAT_NUM_PREDICT` (default `400`, vs. analyzer's `300`) — freeform prose gets a larger token budget than the fixed JSON schema used for per-target insights.
  - New `build_target_insight_summaries()` — loops all targets, defaulting to `status="Unknown"` for targets with no insight yet.
  - New `stream_chat_response()` — assembles context, delegates to `llm.stream_chat`, wired as `app.state.stream_chat`.
- `api.py`: new `POST /chat` endpoint accepting a `ChatIn` pydantic model (`message`, optional `history`), returning a `StreamingResponse` over the generator (headers sent immediately, chunks forwarded to client as-is).

**Helm chart:**
- `chart/templates/agent-deployment.yaml` and `chart/values.yaml`: new `ollama.llm.chatNumPredict` config key, rendered as an env var on the agent Deployment matching the pattern for existing `LLM_NUM_PREDICT`.

### Frontend (`services/ui/pages/1_Chat.py`)

- `st.session_state.chat_messages` holds full on-screen conversation history (one dict per message with `role` and `content`).
- On user input via `st.chat_input()`: appends user message, displays it, then calls `stream_chat(message, history)` generator which calls `POST /chat` with `requests` and yields decoded chunks from `response.iter_content(chunk_size=None, decode_unicode=True)`.
- Only the last 3 message exchanges (6 messages) from full client-side history are sent to the API as `history` — the full session stays on the client for display, but abbreviated history is sent to the model to stay within the CPU model's `num_ctx=2048` budget.
- A `st.spinner()` wraps pulling the first chunk before handing the rest to `st.write_stream` (small `chunks_with_first()` wrapper re-prepends it) — added after real-world timing showed ~100+ seconds to first token due to CPU prompt evaluation.

## Why This Design, and What Was Rejected

**UI stays HTTP-only:** Streamlit never touches Ollama or SQLite directly, maintaining the existing "UI talks only to FastAPI" boundary. The `/chat` endpoint is the sole new surface.

**Context built fresh server-side each turn, not full history to the model:** Per the original phase plan, to stay within the 3B model's small `num_ctx` budget. Per-target latest stored insights are reused (already LLM-condensed) rather than re-running rule checks or re-summarizing findings for every chat turn.

**Separate freeform prompt path rather than reusing `analyze()`:** The existing `analyze()` enforces a fixed JSON schema (`status`/`issues`/`recommendation`); conversational prose is a different shape, requiring its own system prompt and call path rather than bending the existing structure.

**`pages/` directory over restructuring `app.py` into tabs:** Kept as a completely separate file, lowering risk to the existing working dashboard (zero changes to `services/ui/app.py`).

**Client-side spinner for first-token latency:** Added only after real-world measurement showed prompt evaluation with fuller chat context can take 100+ seconds before the first token, long enough that a bare empty `st.write_stream` would appear to hang.

## Verification

- `python3 -m py_compile` on all changed/new Python files; `helm template ./chart` confirmed the new env var renders correctly.
- Deployed via `make deploy` to the `ai-eventer` minikube profile; agent logs confirmed rollout with no errors.
- Streaming verified two ways against live agent (port-forwarded locally):
  1. `curl -N` with timing showed headers immediately (streaming start) vs. total response time 100+ seconds — genuine stream, not buffered.
  2. Custom Python script timestamping each chunk via `iter_content()` confirmed token-by-token delivery (~0.2-0.4s between tokens) after ~100-110 second gap for CPU prompt evaluation.
  3. Context correctness verified: asking about a specific watch target by name returned an answer citing that target's correct namespace, selector, and current stored status — not hallucinated.

**Known limitation:** No browser automation (Playwright, chromium-cli) available in dev environment. The Streamlit UI itself — spinner display, `st.write_stream` rendering, sidebar navigation — was **not** click-tested in a real browser. User must verify manually via `kubectl port-forward` → http://localhost:8501 → "Chat" sidebar link.

## Update 2026-08-18: speed and answer-quality fixes after real browser testing

Two problems surfaced once the user actually tried the chat page:

1. **Latency worse than expected.** `kubectl top pod` showed the Ollama pod pegged at ~1.8 of its
   2-core CPU limit and ~4Gi of its 4Gi memory limit, while the node had 14 cores / ~24Gi
   allocatable and mostly idle - the model was CPU-throttled, not compute-starved on the host.
   Bumped `chart/values.yaml`'s `ollama.resources` from `requests: {cpu: 1, memory: 2Gi} / limits:
   {cpu: 2, memory: 4Gi}` to `requests: {cpu: 2, memory: 2Gi} / limits: {cpu: 4, memory: 5Gi}`.
   Measured first-token latency dropped from ~107s to ~64-74s on the same prompt shape (Ollama
   auto-detects available CPU threads on process start, so no other config was needed). Still
   CPU-bound and still slow in absolute terms - this is the ceiling of a 3B model on CPU-only
   inference, not a bug, but the resource limit was an unnecessary extra tax on top of that.
2. **Overly literal refusals.** Asked "what's the cluster state?", the model replied that a
   "cluster description" wasn't in the provided context - even though the per-target status list
   *is* that description. The original `CHAT_SYSTEM_PROMPT`'s "say you don't know rather than
   guess" instruction was being applied too broadly. Fixed in `llm.py`:
   - `build_chat_context()` now prepends a deterministic `Overall: N watch target(s) - X Healthy,
     Y Degraded, ...` rollup line, computed in Python (counting `target_insights` by status) -
     the same "pre-aggregate in code, let the model narrate" philosophy already used for rule
     findings, rather than expecting a 3B model to count/summarize a list correctly on its own.
   - `CHAT_SYSTEM_PROMPT` now explicitly states the CONTEXT block *is* the current cluster state
     and general-overview questions are always answerable from it; the "decline if not covered"
     instruction is scoped to genuinely absent specifics (an unlisted target/resource name).
   - Added an explicit "reply in the same language as the operator's question, don't mix
     languages" instruction, prompted by an observed mixed-script glitch (a stray CJK character
     mid-sentence in a Russian reply) - a known small-model artifact on code-switched prompts,
     not something fixable beyond nudging via instruction.
   Verified via the same live-target test used for the OOM prompt fix earlier in this project:
   after forcing a fresh analysis (real `Critical`/`Degraded` insights in the DB), asking for a
   cluster overview produced a direct, correct synthesis instead of a refusal.

## Update 2026-08-18 (part 2): diagnosing "why is this still slow"

After the CPU bump above, the user still saw wide latency variance (sub-second to ~45s) and asked
whether it was the small model or resource sizing. Measured directly against the live agent:

- **Ollama's default 5-minute idle unload was the other hidden cost.** Every `make deploy` in this
  session restarted the Ollama pod, so the *first* request afterward always paid a full cold
  model-load penalty (~60-100s) on top of normal inference - this, not steady-state inference
  speed, was most of what made testing feel uniformly slow. Fixed: added `OLLAMA_KEEP_ALIVE: "-1"`
  env var on the Ollama container in `chart/templates/ollama-deployment.yaml` (confirmed via
  Context7 docs: negative value keeps a model loaded indefinitely; memory for it is already
  reserved via `ollama.resources`, so this costs nothing extra). Verified: first request after a
  restart still took ~64s (expected, unavoidable one-time cost), but a second request immediately
  after came back in **0.93s**.
- **Remaining variance is prompt-prefix cache behavior, not a bug.** A third request, moments
  later, took ~43s again. Cause: `build_chat_context()` includes `db.recent_events_summary_all()`
  - live, timestamp-sensitive cluster events - in every prompt. Ollama/llama.cpp can reuse the KV
  cache for an unchanged prompt prefix (explaining the 0.93s case), but any change to the events
  list (expected here, since the `test-app` fixture's crash-loop/OOM Deployments generate new
  events every few seconds) invalidates that cache and forces full prompt re-evaluation.
- **Conclusion, given directly to the user**: the ~10-45s floor for a cache-miss request is the
  real, current cost of a 3B model on CPU-only inference plus always including fresh event data -
  not further reducible without a real tradeoff (more CPU with diminishing returns, a smaller/
  faster model for chat specifically at a quality cost, or coarser event-timestamp bucketing to
  improve cache-hit odds at a freshness cost). **User's explicit choice: leave as-is** - the two
  fixes above (CPU limit, `KEEP_ALIVE`) already removed the artificial overhead; the rest is an
  accepted tradeoff, not a bug to keep chasing.

## What to Know for Maintenance

- Chat history is entirely client-side (`st.session_state`), not persisted to SQLite — browser refresh or Streamlit pod restart loses conversation. This is deliberate scope (single-operator tool, not a multi-viewer product); revisit if that assumption changes.
- The ~100+ second first-token latency is a direct consequence of CPU-only inference on prompt size (per-target summaries + events + system prompt). Adding more watch targets or a much larger `recent_events_summary_all` limit will make this slower and may exceed `num_ctx=2048` — worth revisiting `CHAT_NUM_PREDICT`/`LLM_NUM_CTX` if that happens.
- `stream_chat()`'s try/except in `llm.py` catches broad `Exception` around the whole streaming loop, matching `analyze()`'s convention — if Ollama dies mid-stream (after first chunk sent), the partial response stays on the client with the error appended, rather than being retracted.
