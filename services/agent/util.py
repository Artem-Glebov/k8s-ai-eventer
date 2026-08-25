from datetime import timezone


def to_sqlite_ts(dt) -> str | None:
    """Normalize a k8s timestamp (tz-aware datetime) to the same
    'YYYY-MM-DD HH:MM:SS' format SQLite's datetime('now') produces,
    so string comparisons in WHERE clauses stay correct."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
