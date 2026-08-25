"""Single-process SQLite access. One connection, WAL mode, writes serialized
behind a lock so the watch loop, analyzer loop, and FastAPI reads can share
one file safely without needing separate writer processes/pods."""

import json
import sqlite3
import threading
from contextlib import contextmanager

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE,
    namespace TEXT NOT NULL,
    involved_kind TEXT,
    involved_name TEXT,
    reason TEXT,
    message TEXT,
    type TEXT,
    count INTEGER,
    first_seen TEXT,
    last_seen TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_ns_name ON events(namespace, involved_name);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    resource_name TEXT,
    check_name TEXT NOT NULL,
    detail TEXT,
    remediation TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_findings_target ON findings(target_name);

CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_name TEXT NOT NULL,
    status TEXT,
    issues TEXT,
    recommendation TEXT,
    raw TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_insights_target ON insights(target_name, created_at);

-- Creative mode: one row per periodic unscoped sweep of every workload
-- (Deployment/StatefulSet/DaemonSet) in scope (not tied to a watch target, so
-- it doesn't fit the `insights` table's target_name-keyed shape).
-- deployments_scanned/namespaces_scanned/duration_ms exist so the UI can show
-- what the sweep actually cost, not just its result. Column name predates the
-- 3-kind generalization (was Deployment-only) and now stores a generic
-- workload count - kept as-is to avoid a migration for a cosmetic rename.
CREATE TABLE IF NOT EXISTS cluster_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT,
    issues TEXT,
    recommendation TEXT,
    raw TEXT,
    deployments_scanned INTEGER,
    namespaces_scanned TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cluster_insights_created ON cluster_insights(created_at);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    namespace TEXT NOT NULL,
    selector_kind TEXT NOT NULL,
    selector_name TEXT NOT NULL,
    instruction TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Live set of resource names (Deployment name + its current pods) a target's
-- events should match against. Refreshed wholesale every analyzer tick from
-- the label-selector pod lookup rules.py already does, since pod names churn
-- on every rollout/restart and can't be matched by a static selector_name.
CREATE TABLE IF NOT EXISTS target_resources (
    target_name TEXT NOT NULL,
    namespace TEXT NOT NULL,
    resource_name TEXT NOT NULL,
    UNIQUE(target_name, resource_name)
);
CREATE INDEX IF NOT EXISTS idx_target_resources_lookup ON target_resources(namespace, resource_name);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    notify_on_critical INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Debounce state for Critical-transition emails, one row per watch target.
-- last_status is this target's status as of the previous analyzer tick (not
-- read from `insights` - that table isn't reliably tick-aligned once pruning
-- runs). last_notified_at is set only on a tick that actually sent an email,
-- so a target stuck at Critical doesn't re-fire every tick, but a
-- Critical -> recovered -> Critical flap fires again.
CREATE TABLE IF NOT EXISTS notification_state (
    target_name TEXT PRIMARY KEY NOT NULL,
    last_status TEXT NOT NULL,
    last_notified_at TEXT
);
"""


def init_db(path: str) -> None:
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(SCHEMA)
    # Lightweight migration for pre-existing DBs from before this column
    # existed - CREATE TABLE IF NOT EXISTS above doesn't add columns to an
    # already-created table.
    try:
        _conn.execute("ALTER TABLE findings ADD COLUMN remediation TEXT")
    except sqlite3.OperationalError:
        pass  # already added by a previous startup
    _conn.commit()


@contextmanager
def write():
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    _conn.row_factory = sqlite3.Row
    cur = _conn.execute(sql, params)
    return cur.fetchall()


def upsert_event(uid, namespace, involved_kind, involved_name, reason, message, type_, count, first_seen, last_seen):
    with write() as conn:
        conn.execute(
            """
            INSERT INTO events (uid, namespace, involved_kind, involved_name, reason, message, type, count, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                message=excluded.message, count=excluded.count, last_seen=excluded.last_seen
            """,
            (uid, namespace, involved_kind, involved_name, reason, message, type_, count, first_seen, last_seen),
        )


def insert_findings(target_name: str, namespace: str, findings: list[dict]) -> None:
    with write() as conn:
        conn.executemany(
            "INSERT INTO findings (target_name, namespace, resource_name, check_name, detail, remediation) VALUES (?, ?, ?, ?, ?, ?)",
            [(target_name, namespace, f.get("resource_name"), f["check_name"], f.get("detail"), f.get("remediation")) for f in findings],
        )


def insert_insight(target_name: str, status: str, issues: list[str], recommendation: str, raw: str) -> None:
    with write() as conn:
        conn.execute(
            "INSERT INTO insights (target_name, status, issues, recommendation, raw) VALUES (?, ?, ?, ?, ?)",
            (target_name, status, json.dumps(issues), recommendation, raw),
        )


def insert_cluster_insight(
    status: str, issues: list[str], recommendation: str, raw: str,
    workloads_scanned: int, namespaces_scanned: list[str], duration_ms: int,
) -> None:
    with write() as conn:
        conn.execute(
            """
            INSERT INTO cluster_insights
                (status, issues, recommendation, raw, deployments_scanned, namespaces_scanned, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (status, json.dumps(issues), recommendation, raw, workloads_scanned,
             json.dumps(namespaces_scanned), duration_ms),
        )


def latest_cluster_insight() -> sqlite3.Row | None:
    rows = query("SELECT * FROM cluster_insights ORDER BY created_at DESC LIMIT 1")
    return rows[0] if rows else None


def upsert_target(name, namespace, selector_kind, selector_name, instruction) -> None:
    with write() as conn:
        conn.execute(
            """
            INSERT INTO targets (name, namespace, selector_kind, selector_name, instruction)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                namespace=excluded.namespace, selector_kind=excluded.selector_kind,
                selector_name=excluded.selector_name, instruction=excluded.instruction
            """,
            (name, namespace, selector_kind, selector_name, instruction),
        )


def list_targets() -> list[sqlite3.Row]:
    return query("SELECT * FROM targets ORDER BY name")


def set_target_resources(target_name: str, namespace: str, resource_names: list[str]) -> None:
    with write() as conn:
        conn.execute("DELETE FROM target_resources WHERE target_name = ?", (target_name,))
        conn.executemany(
            "INSERT OR IGNORE INTO target_resources (target_name, namespace, resource_name) VALUES (?, ?, ?)",
            [(target_name, namespace, name) for name in resource_names],
        )


def get_target(name: str) -> sqlite3.Row | None:
    rows = query("SELECT * FROM targets WHERE name = ?", (name,))
    return rows[0] if rows else None


def delete_target(name: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM targets WHERE name = ?", (name,))
        conn.execute("DELETE FROM target_resources WHERE target_name = ?", (name,))
        conn.execute("DELETE FROM findings WHERE target_name = ?", (name,))
        conn.execute("DELETE FROM insights WHERE target_name = ?", (name,))
        conn.execute("DELETE FROM notification_state WHERE target_name = ?", (name,))


def recent_event_summaries(target_name: str, minutes: int = 20, limit: int = 20) -> list[str]:
    rows = query(
        """
        SELECT e.reason, e.involved_kind, e.involved_name, e.count, e.last_seen
        FROM events e
        JOIN target_resources tr ON tr.namespace = e.namespace AND tr.resource_name = e.involved_name
        WHERE tr.target_name = ? AND e.last_seen >= datetime('now', ?)
        ORDER BY e.last_seen DESC
        LIMIT ?
        """,
        (target_name, f"-{minutes} minutes", limit),
    )
    return [
        f"{r['reason']} on {r['involved_kind']}/{r['involved_name']} x{r['count']}, last seen {r['last_seen']}"
        for r in rows
    ]


def recent_events_summary_all(minutes: int = 20, limit: int = 30) -> list[str]:
    rows = query(
        """
        SELECT reason, involved_kind, involved_name, namespace, count, last_seen
        FROM events
        WHERE last_seen >= datetime('now', ?)
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (f"-{minutes} minutes", limit),
    )
    return [
        f"{r['reason']} on {r['involved_kind']}/{r['involved_name']} in {r['namespace']} x{r['count']}, last seen {r['last_seen']}"
        for r in rows
    ]


def events_for_target(target_name: str, limit: int = 100) -> list[sqlite3.Row]:
    return query(
        """
        SELECT e.* FROM events e
        JOIN target_resources tr ON tr.namespace = e.namespace AND tr.resource_name = e.involved_name
        WHERE tr.target_name = ?
        ORDER BY e.last_seen DESC LIMIT ?
        """,
        (target_name, limit),
    )


def findings_for_target(target_name: str, limit: int = 50) -> list[sqlite3.Row]:
    return query(
        "SELECT * FROM findings WHERE target_name = ? ORDER BY created_at DESC LIMIT ?",
        (target_name, limit),
    )


def latest_insight(target_name: str) -> sqlite3.Row | None:
    rows = query(
        "SELECT * FROM insights WHERE target_name = ? ORDER BY created_at DESC LIMIT 1",
        (target_name,),
    )
    return rows[0] if rows else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def upsert_user_password(
    username: str, display_name: str, email: str, password_hash: str, notify_on_critical: bool = True,
) -> None:
    with write() as conn:
        conn.execute(
            """
            INSERT INTO users (username, display_name, email, password_hash, notify_on_critical)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                display_name=excluded.display_name, email=excluded.email,
                password_hash=excluded.password_hash, notify_on_critical=excluded.notify_on_critical
            """,
            (username, display_name, email, password_hash, int(notify_on_critical)),
        )


def list_users() -> list[sqlite3.Row]:
    return query("SELECT * FROM users ORDER BY username")


def get_user(username: str) -> sqlite3.Row | None:
    rows = query("SELECT * FROM users WHERE username = ?", (username,))
    return rows[0] if rows else None


def count_users() -> int:
    return query("SELECT COUNT(*) AS n FROM users")[0]["n"]


def update_user_profile(username: str, display_name: str, email: str, notify_on_critical: bool) -> None:
    with write() as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, email = ?, notify_on_critical = ? WHERE username = ?",
            (display_name, email, int(notify_on_critical), username),
        )


def update_user_password(username: str, password_hash: str) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))


def delete_user(username: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))


def get_notification_state(target_name: str) -> sqlite3.Row | None:
    rows = query("SELECT * FROM notification_state WHERE target_name = ?", (target_name,))
    return rows[0] if rows else None


def set_notification_state(target_name: str, status: str, notified: bool) -> None:
    with write() as conn:
        if notified:
            conn.execute(
                """
                INSERT INTO notification_state (target_name, last_status, last_notified_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(target_name) DO UPDATE SET
                    last_status=excluded.last_status, last_notified_at=excluded.last_notified_at
                """,
                (target_name, status),
            )
        else:
            conn.execute(
                """
                INSERT INTO notification_state (target_name, last_status)
                VALUES (?, ?)
                ON CONFLICT(target_name) DO UPDATE SET last_status=excluded.last_status
                """,
                (target_name, status),
            )


def notification_recipients() -> list[str]:
    rows = query("SELECT email FROM users WHERE notify_on_critical = 1")
    return [r["email"] for r in rows]


def prune(retention_days: int) -> None:
    with write() as conn:
        conn.execute("DELETE FROM events WHERE created_at < datetime('now', ?)", (f"-{retention_days} days",))
        conn.execute("DELETE FROM insights WHERE created_at < datetime('now', ?)", (f"-{retention_days} days",))
        conn.execute("DELETE FROM cluster_insights WHERE created_at < datetime('now', ?)", (f"-{retention_days} days",))
