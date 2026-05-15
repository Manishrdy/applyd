"""HTML routes for the admin panel.

One handler per page, each rendering its dedicated template. The data
fetching here is intentionally minimal — pages hydrate themselves from
the JSON admin APIs via Alpine.js, mirroring the rest of the dashboard.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from app.admin.deps import AdminUser, require_admin_user
from app.admin.services import backups as backup_service


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


router = APIRouter()


def _ctx(request: Request, admin: AdminUser, **extra) -> dict:
    base = {
        "admin": admin,
        "csrf_token": request.cookies.get("applyd_csrf", ""),
    }
    base.update(extra)
    return base


@router.get("/admin")
def admin_home(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request, "admin/home.html", _ctx(request, admin, active_page="home"),
    )


@router.get("/admin/auth-log")
def admin_auth_log(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request, "admin/auth_log.html", _ctx(request, admin, active_page="auth_log"),
    )


@router.get("/admin/sessions")
def admin_sessions_page(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request, "admin/sessions.html", _ctx(request, admin, active_page="sessions"),
    )


@router.get("/admin/rate-limits")
def admin_rate_limits_page(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request, "admin/rate_limits.html", _ctx(request, admin, active_page="rate_limits"),
    )


@router.get("/admin/maintenance")
def admin_maintenance_page(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request, "admin/maintenance.html", _ctx(request, admin, active_page="maintenance"),
    )


@router.get("/admin/backups")
def admin_backups_page(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request,
        "admin/backups.html",
        _ctx(
            request,
            admin,
            active_page="backups",
            token_configured=backup_service.is_token_configured(),
        ),
    )


@router.get("/admin/audit")
def admin_audit_page(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request, "admin/audit.html", _ctx(request, admin, active_page="audit"),
    )
