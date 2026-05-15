"""Admin catchall — last router registered.

Any GET to /admin/<anything-we-didnt-route> renders a friendly 404 inside
the admin chrome (rather than the public 404). Any /api/admin/<anything>
returns a JSON 404. We still require admin auth — we don't want to leak
"this is the admin namespace" to logged-out users.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.admin.deps import AdminUser, require_admin_user


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


router = APIRouter()


@router.api_route("/admin/{rest:path}", methods=["GET", "POST"])
def admin_catchall(rest: str, request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request,
        "admin/not_found.html",
        {"admin": admin, "csrf_token": request.cookies.get("applyd_csrf", ""), "path": f"/admin/{rest}"},
        status_code=404,
    )


@router.api_route("/api/admin/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def admin_api_catchall(rest: str, admin: AdminUser = Depends(require_admin_user)):
    return JSONResponse({"detail": f"unknown admin endpoint: /api/admin/{rest}"}, status_code=404)
