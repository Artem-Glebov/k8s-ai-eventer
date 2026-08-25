import bcrypt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db
from rules import KIND_INFO, SUPPORTED_SELECTOR_KINDS

app = FastAPI(title="ai-k8s-eventer agent")


class TargetIn(BaseModel):
    namespace: str
    selector_kind: str = "deployment"
    selector_name: str
    instruction: str


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []


class UserIn(BaseModel):
    display_name: str
    email: str
    password: str
    notify_on_critical: bool = True


class UserProfileIn(BaseModel):
    display_name: str
    email: str
    notify_on_critical: bool


class PasswordIn(BaseModel):
    password: str


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/targets")
def targets():
    return db.rows_to_dicts(db.list_targets())


@app.put("/targets/{name}")
def upsert_target(name: str, t: TargetIn):
    if t.selector_kind not in SUPPORTED_SELECTOR_KINDS:
        raise HTTPException(
            status_code=400, detail=f"selector_kind must be one of {sorted(SUPPORTED_SELECTOR_KINDS)}",
        )
    db.upsert_target(
        name=name, namespace=t.namespace, selector_kind=t.selector_kind,
        selector_name=t.selector_name, instruction=t.instruction,
    )
    return dict(db.get_target(name))


@app.delete("/targets/{name}")
def delete_target(name: str):
    if not db.get_target(name):
        raise HTTPException(status_code=404, detail=f"unknown target: {name}")
    db.delete_target(name)
    return {"deleted": name}


@app.post("/targets/{name}/analyze")
def analyze_target_now(name: str, request: Request):
    t = db.get_target(name)
    if not t:
        raise HTTPException(status_code=404, detail=f"unknown target: {name}")
    return request.app.state.analyze_one_target(t)


@app.post("/chat")
def chat(body: ChatIn, request: Request):
    return StreamingResponse(
        request.app.state.stream_chat(body.message, body.history),
        media_type="text/plain",
    )


@app.get("/cluster-insights/latest")
def cluster_insights_latest(request: Request):
    row = db.latest_cluster_insight()
    return {"enabled": request.app.state.creative_mode_enabled, "insight": dict(row) if row else None}


@app.post("/cluster-insights/scan")
def cluster_insights_scan(request: Request):
    # Manual trigger works regardless of whether the interval loop is
    # running - useful for testing without waiting for creativeMode.intervalSeconds.
    return request.app.state.run_creative_mode_tick()


@app.get("/namespaces")
def namespaces(request: Request):
    # Live listing needs cluster-scoped RBAC on `namespaces`; under
    # rbac.scope=namespaces a per-namespace RoleBinding can't grant that, so
    # fall back to the configured set - the only namespaces the agent can see.
    if request.app.state.rbac_scope == "cluster":
        return sorted(ns.metadata.name for ns in request.app.state.core_v1.list_namespace().items)
    return sorted(request.app.state.watch_namespaces)


@app.get("/namespaces/{namespace}/workloads")
def workloads(namespace: str, request: Request, kind: str = "deployment"):
    if kind not in SUPPORTED_SELECTOR_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(SUPPORTED_SELECTOR_KINDS)}")
    list_method = getattr(request.app.state.apps_v1, KIND_INFO[kind]["list_namespaced"])
    items = list_method(namespace).items
    return sorted(w.metadata.name for w in items)


@app.get("/events")
def events(target: str, limit: int = 100):
    t = db.get_target(target)
    if not t:
        raise HTTPException(status_code=404, detail=f"unknown target: {target}")
    return db.rows_to_dicts(db.events_for_target(target, limit))


@app.get("/findings")
def findings(target: str, limit: int = 50):
    t = db.get_target(target)
    if not t:
        raise HTTPException(status_code=404, detail=f"unknown target: {target}")
    return db.rows_to_dicts(db.findings_for_target(target, limit))


@app.get("/insights/latest")
def insights_latest(target: str):
    t = db.get_target(target)
    if not t:
        raise HTTPException(status_code=404, detail=f"unknown target: {target}")
    row = db.latest_insight(target)
    return dict(row) if row else None


# NOTE: this endpoint (and every /users endpoint below) has no auth of its
# own - it's reachable by anything with cluster network access to this
# ClusterIP Service, same trust boundary as every other endpoint in this
# file. Only the Streamlit UI gets a login gate; the agent API itself is a
# deliberately separate, larger effort left for later. password_hash is a
# bcrypt hash (safe to disclose by design) and is included here because this
# same endpoint feeds the UI's streamlit-authenticator credentials dict - a
# second "hash-stripped" endpoint would add code without changing the actual
# trust boundary.
@app.get("/users")
def users():
    return db.rows_to_dicts(db.list_users())


@app.post("/users/{username}")
def create_user(username: str, u: UserIn):
    if db.get_user(username):
        raise HTTPException(status_code=409, detail=f"user already exists: {username}")
    password_hash = bcrypt.hashpw(u.password.encode(), bcrypt.gensalt()).decode()
    db.upsert_user_password(username, u.display_name, u.email, password_hash, u.notify_on_critical)
    return dict(db.get_user(username))


@app.put("/users/{username}/profile")
def update_user_profile(username: str, u: UserProfileIn):
    if not db.get_user(username):
        raise HTTPException(status_code=404, detail=f"unknown user: {username}")
    db.update_user_profile(username, u.display_name, u.email, u.notify_on_critical)
    return dict(db.get_user(username))


@app.put("/users/{username}/password")
def update_user_password(username: str, p: PasswordIn):
    if not db.get_user(username):
        raise HTTPException(status_code=404, detail=f"unknown user: {username}")
    password_hash = bcrypt.hashpw(p.password.encode(), bcrypt.gensalt()).decode()
    db.update_user_password(username, password_hash)
    return {"updated": username}


@app.delete("/users/{username}")
def delete_user(username: str):
    if not db.get_user(username):
        raise HTTPException(status_code=404, detail=f"unknown user: {username}")
    if db.count_users() <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last remaining user - would lock everyone out")
    db.delete_user(username)
    return {"deleted": username}
