"""Admin API for reading the admin_audit log."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.admin import audit as audit_service
from app.admin.deps import AdminUser, require_admin_user


router = APIRouter()


@router.get("/audit")
def list_audit(
    limit: int = 100,
    action: str | None = None,
    target: str | None = None,
    admin: AdminUser = Depends(require_admin_user),
):
    entries = audit_service.list_recent(limit=limit, action=action, target=target)
    return [audit_service.to_dict(e) for e in entries]
