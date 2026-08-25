import os
import sys

import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auth import require_login

AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000")
HISTORY_TURNS_SENT = 3  # bound what's sent to the model - not the full session

st.set_page_config(page_title="ai-k8s-eventer - Chat", layout="wide")
require_login()
st.title("Chat")
st.caption("Ask about current cluster state - answers are grounded in each watch target's latest insight and recent events, not full chat history.")

st.session_state.setdefault("chat_messages", [])


def stream_chat(message: str, history: list[dict]):
    try:
        with requests.post(
            f"{AGENT_API_URL}/chat",
            json={"message": message, "history": history},
            stream=True,
            timeout=(5, 300),
        ) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
                if chunk:
                    yield chunk
    except requests.RequestException as e:
        yield f"_(agent API unreachable: {e})_"


for msg in st.session_state.chat_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("Ask about the cluster..."):
    st.session_state.chat_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    history = st.session_state.chat_messages[-(HISTORY_TURNS_SENT * 2 + 1):-1]
    with st.chat_message("assistant"):
        # CPU prompt evaluation on a 3B model can take a minute or more before
        # the first token arrives - without a spinner that looks like a hang.
        gen = stream_chat(prompt, history)
        with st.spinner("Thinking... (CPU inference, can take a minute or more)"):
            first_chunk = next(gen, "")

        def chunks_with_first():
            if first_chunk:
                yield first_chunk
            yield from gen

        response = st.write_stream(chunks_with_first())
    st.session_state.chat_messages.append({"role": "assistant", "content": response})
