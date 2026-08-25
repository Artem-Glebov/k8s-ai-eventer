import os
import sys

import requests
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auth import get_authenticator, require_login

AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000")

st.set_page_config(page_title="ai-k8s-eventer - Account", layout="wide")
username = require_login()
st.title("Account")


def api_get(path: str, **params):
    try:
        r = requests.get(f"{AGENT_API_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"agent API unreachable: {e}")
        return None


def api_post(path: str, body: dict):
    try:
        r = requests.post(f"{AGENT_API_URL}{path}", json=body, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        st.error(e.response.json().get("detail", str(e)))
        return None
    except requests.RequestException as e:
        st.error(f"failed to create user: {e}")
        return None


def api_put(path: str, body: dict):
    try:
        r = requests.put(f"{AGENT_API_URL}{path}", json=body, timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        st.error(f"failed to save: {e}")
        return None


def api_delete(path: str):
    try:
        r = requests.delete(f"{AGENT_API_URL}{path}", timeout=5)
        r.raise_for_status()
        return True
    except requests.HTTPError as e:
        st.error(e.response.json().get("detail", str(e)))
        return False
    except requests.RequestException as e:
        st.error(f"failed to delete user: {e}")
        return False


all_users = api_get("/users") or []
me = next((u for u in all_users if u["username"] == username), None)

with st.expander("My profile", expanded=True):
    if me:
        display_name = st.text_input("Display name", value=me["display_name"], key="my_display_name")
        email = st.text_input("Email", value=me["email"], key="my_email")
        notify = st.checkbox(
            "Email me when a watch target goes Critical",
            value=bool(me["notify_on_critical"]), key="my_notify",
        )
        if st.button("Save profile"):
            if api_put(f"/users/{username}/profile", {
                "display_name": display_name, "email": email, "notify_on_critical": notify,
            }):
                get_authenticator(force_refresh=True)
                st.success("Saved")
                st.rerun()

    st.subheader("Change password")
    new_password = st.text_input("New password", type="password", key="pw1")
    confirm_password = st.text_input("Confirm new password", type="password", key="pw2")
    if st.button("Change password"):
        if not new_password or new_password != confirm_password:
            st.error("Passwords are empty or don't match")
        elif api_put(f"/users/{username}/password", {"password": new_password}):
            get_authenticator(force_refresh=True)
            st.success("Password changed")

with st.expander("Manage users"):
    st.dataframe(
        [{"username": u["username"], "display_name": u["display_name"], "email": u["email"],
          "notifications": bool(u["notify_on_critical"])} for u in all_users],
        use_container_width=True,
    )

    st.subheader("Add user")
    new_username = st.text_input("Username", key="new_username")
    new_display_name = st.text_input("Display name", key="new_display_name")
    new_email = st.text_input("Email", key="new_email")
    new_user_password = st.text_input("Password", type="password", key="new_user_password")
    new_notify = st.checkbox("Receives Critical-transition emails", value=True, key="new_notify")
    if st.button("Create user"):
        if not new_username or not new_user_password:
            st.error("Username and password are required")
        elif api_post(f"/users/{new_username}", {
            "display_name": new_display_name, "email": new_email,
            "password": new_user_password, "notify_on_critical": new_notify,
        }):
            get_authenticator(force_refresh=True)
            st.success(f"Created {new_username}")
            st.rerun()

    st.subheader("Remove user")
    other_usernames = [u["username"] for u in all_users if u["username"] != username]
    if other_usernames:
        target_username = st.selectbox("Username to remove", other_usernames)
        if st.button("Delete user"):
            if api_delete(f"/users/{target_username}"):
                get_authenticator(force_refresh=True)
                st.rerun()
    else:
        st.caption("No other users to remove.")
