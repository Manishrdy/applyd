"""Admin API for rate-limit policy + lockout management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.admin.services import identity as identity_client


router = APIRouter()


@router.get("/rate-limits")
async def list_rate_limits(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    try:
        return await identity_client.list_rate_limits(request)
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=502, detail=exc.detail)


@router.post("/rate-limits/policy")
async def update_policy(
    request: Request,
    csrf_token: str = Form(""),
    pair_max: int = Form(...),
    email_max: int = Form(...),
    ip_max: int = Form(...),
    window_seconds: int = Form(...),
    lockout_seconds: int = Form(...),
    admin: AdminUser = Depends(require_admin_user),
):
    try:
        result = await identity_client.update_rate_limit_policy(
            request,
            csrf_token,
            pair_max=pair_max,
            email_max=email_max,
            ip_max=ip_max,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
        )
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    audit.record(
        admin=admin,
        action="update_rate_limit_policy",
        detail=result.get("policy") if isinstance(result, dict) else None,
        request=request,
    )
    return result


@router.post("/rate-limits/unlock")
async def unlock_bucket(
    request: Request,
    csrf_token: str = Form(""),
    bucket_key: str = Form(...),
    admin: AdminUser = Depends(require_admin_user),
):
    try:
        result = await identity_client.unlock_rate_limit_bucket(request, csrf_token, bucket_key)
    except identity_client.IdentityServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    audit.record(
        admin=admin,
        action="unlock_rate_limit_bucket",
        target=bucket_key,
        request=request,
    )
    return result
