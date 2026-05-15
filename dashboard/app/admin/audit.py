"""Admin audit log — record every state-changing admin action.

Keep this module small and stable: it's called from every admin handler,
so a bug here breaks the entire admin surface. The public API is just
`record()` for writers and `list_recent()` for the audit viewer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import Request

from app.admin.deps import AdminUser
from app.database import get_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    id: int
    admin_user_id: int
    admin_email: str
    action: str
    target: str | None
    detail: str | None
    ip_address: str | None
    user_agent: str | None
    created_at: str


def _client_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


def _user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("user-agent")


def _serialize_detail(detail: Any) -> str | None:
    if detail is None:
        return None
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(detail)


def record(
    *,
    admin: AdminUser,
    action: str,
    target: str | None = None,
    detail: Any = None,
    request: Request | None = None,
) -> None:
    """Insert one audit row. Never raises — admin actions must not abort here."""
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO admin_audit "
                "(admin_user_id, admin_email, action, target, detail, ip_address, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    admin.id,
                    admin.email,
                    action,
                    target,
                    _serialize_detail(detail),
                    _client_ip(request),
                    _user_agent(request),
                ),
            )
    except Exception:
        # Log loudly but do not bubble — losing one audit row is worse than
        # surfacing a 500 on the action that triggered it. Pair this with
        # an alert on the log to catch persistent failures.
        log.exception("admin_audit insert failed action=%s target=%s", action, target)


def list_recent(
    *,
    limit: int = 100,
    action: str | None = None,
    target: str | None = None,
) -> list[AuditEntry]:
    """Read the audit log, newest first. Caller bounds the page size."""
    limit = max(1, min(int(limit), 500))
    sql = "SELECT id, admin_user_id, admin_email, action, target, detail, ip_address, user_agent, created_at FROM admin_audit"
    clauses: list[str] = []
    params: list[Any] = []
    if action:
        clauses.append("action = ?")
        params.append(action)
    if target:
        clauses.append("target = ?")
        params.append(target)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [AuditEntry(**dict(r)) for r in rows]


def to_dict(entry: AuditEntry) -> dict:
    return asdict(entry)
