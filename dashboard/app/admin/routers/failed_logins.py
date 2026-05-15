"""Admin API for failed-login viewer + bulk clear."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.admin.services import identity as identity_client


router = APIRouter()


@router.get("/failed-logins")
async def list_failed_logins(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
    limit: int = 100,
):
    try:
        return await identity_client.list_failed_logins(request, limit=limit)
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=502, detail=exc.detail)


@router.post("/failed-logins/clear")
async def clear_failed_logins(
    request: Request,
    csrf_token: str = Form(""),
    email: str = Form(""),
    ip_address: str = Form(""),
    admin: AdminUser = Depends(require_admin_user),
):
    try:
        result = await identity_client.clear_failed_logins(
            request,
            csrf_token,
            email=email.strip() or None,
            ip_address=ip_address.strip() or None,
        )
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    audit.record(
        admin=admin,
        action="clear_failed_logins",
        target=email.strip() or ip_address.strip() or "(all)",
        detail=result,
        request=request,
    )
    return result
