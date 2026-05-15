"""Admin API for session management. Proxies to identity-service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.admin.services import identity as identity_client


router = APIRouter()


@router.get("/sessions")
async def list_sessions(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
    limit: int = 200,
):
    try:
        return await identity_client.list_sessions(request)
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=502, detail=exc.detail)


@router.post("/sessions/{public_id}/terminate")
async def terminate_session(
    public_id: str,
    request: Request,
    csrf_token: str = Form(""),
    admin: AdminUser = Depends(require_admin_user),
):
    try:
        result = await identity_client.terminate_session(request, public_id, csrf_token)
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    audit.record(
        admin=admin,
        action="terminate_session",
        target=public_id,
        request=request,
    )
    return result
