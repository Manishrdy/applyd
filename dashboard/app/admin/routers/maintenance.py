"""Admin API for maintenance-mode toggle."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Form, Request

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.admin.services import maintenance as maintenance_service


router = APIRouter()


@router.get("/maintenance")
def get_maintenance(admin: AdminUser = Depends(require_admin_user)):
    return asdict(maintenance_service.get_status())


@router.post("/maintenance/enable")
def enable_maintenance(
    request: Request,
    message: str = Form(""),
    admin: AdminUser = Depends(require_admin_user),
):
    status = maintenance_service.enable(message=message, enabled_by=admin.email)
    audit.record(
        admin=admin,
        action="enable_maintenance_mode",
        detail={"message": message},
        request=request,
    )
    return asdict(status)


@router.post("/maintenance/disable")
def disable_maintenance(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    status = maintenance_service.disable(enabled_by=admin.email)
    audit.record(
        admin=admin,
        action="disable_maintenance_mode",
        request=request,
    )
    return asdict(status)
