"""Login gate shared by every page in this plain pages/ multipage app - there
is no st.navigation single entrypoint here (Streamlit auto-discovers
pages/*.py and runs each script independently), so a check placed in only
one page would not gate the others. Every page must call require_login()
itself."""

import os

import requests
import streamlit as st
import streamlit_authenticator as stauth

AGENT_API_URL = os.environ.get("AGENT_API_URL", "http://localhost:8000")
COOKIE_NAME = os.environ.get("AUTH_COOKIE_NAME", "ai_eventer_auth")
COOKIE_KEY = os.environ.get("AUTH_COOKIE_KEY", "dev-only-insecure-key")
COOKIE_EXPIRY_DAYS = float(os.environ.get("AUTH_COOKIE_EXPIRY_DAYS", "30"))


def fetch_credentials() -> dict | None:
    try:
        r = requests.get(f"{AGENT_API_URL}/users", timeout=5)
        r.raise_for_status()
        users = r.json()
    except requests.RequestException as e:
        st.error(f"agent API unreachable: {e}")
        return None
    return {
        "usernames": {
            u["username"]: {
                "email": u["email"],
                "first_name": u["display_name"],
                "last_name": "",
                "password": u["password_hash"],
                "roles": [],
            }
            for u in users
        }
    }


def get_authenticator(force_refresh: bool = False) -> "stauth.Authenticate | None":
    # Cached in session_state across a page's widget reruns (re-fetching
    # credentials on every rerun would be wasteful); force_refresh=True right
    # after an Account-page mutation (password change, new/removed user) so
    # the next login check sees current data without a fresh browser session.
    if force_refresh or "authenticator" not in st.session_state:
        credentials = fetch_credentials()
        if credentials is None:
            return None
        st.session_state["authenticator"] = stauth.Authenticate(
            credentials, COOKIE_NAME, COOKIE_KEY, COOKIE_EXPIRY_DAYS, auto_hash=False,
        )
    return st.session_state["authenticator"]


def require_login() -> str:
    """Call at the top of every page script, after st.set_page_config(). Renders
    the login form and st.stop()s the script if not authenticated; otherwise
    renders a logout control and returns the logged-in username."""
    authenticator = get_authenticator()
    if authenticator is None:
        st.stop()

    authenticator.login(location="main")
    status = st.session_state.get("authentication_status")
    if status is False:
        st.error("Username/password is incorrect")
        st.stop()
    elif status is None:
        st.info("Please log in.")
        st.stop()

    authenticator.logout(location="sidebar")
    return st.session_state["username"]
