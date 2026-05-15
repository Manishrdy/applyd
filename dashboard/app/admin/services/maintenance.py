"""Maintenance-mode flag service.

Stored as a single key in `app_maintenance` (the existing KV table). When
on, all non-admin traffic to protected routes gets a 503 with a friendly
message; admins still get through so they can flip the flag back off.

Public API is tiny on purpose:
    - get_status()  -> MaintenanceStatus
    - enable(...)   -> MaintenanceStatus
    - disable(...)  -> MaintenanceStatus
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.database import get_db


_KEY = "maintenance_mode"


@dataclass(frozen=True)
class MaintenanceStatus:
    enabled: bool
    message: str
    updated_at: str | None
    enabled_by: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_row() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value, updated_at FROM app_maintenance WHERE key = ?",
            (_KEY,),
        ).fetchone()
    if row is None or not row["value"]:
        return None
    try:
        payload = json.loads(row["value"])
        if not isinstance(payload, dict):
            return None
    except (TypeError, ValueError):
        return None
    payload["_updated_at"] = row["updated_at"]
    return payload


def get_status() -> MaintenanceStatus:
    """Default: disabled. Never raises — used in hot middleware path."""
    payload = _read_row()
    if payload is None:
        return MaintenanceStatus(enabled=False, message="", updated_at=None, enabled_by=None)
    return MaintenanceStatus(
        enabled=bool(payload.get("enabled")),
        message=str(payload.get("message", "")),
        updated_at=payload.get("_updated_at"),
        enabled_by=payload.get("enabled_by"),
    )


def _write(enabled: bool, message: str, enabled_by: str) -> MaintenanceStatus:
    payload = json.dumps(
        {"enabled": enabled, "message": message, "enabled_by": enabled_by},
        separators=(",", ":"),
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO app_maintenance(key, value, updated_at) "
            "VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (_KEY, payload),
        )
    return get_status()


def enable(message: str, enabled_by: str) -> MaintenanceStatus:
    """Turn maintenance on. The message is shown on the 503 response page."""
    return _write(enabled=True, message=message.strip(), enabled_by=enabled_by)


def disable(enabled_by: str) -> MaintenanceStatus:
    """Turn maintenance off. Message is preserved for audit; flag flips."""
    current = get_status()
    return _write(enabled=False, message=current.message, enabled_by=enabled_by)
