"""Thin HTTP client for identity-service admin APIs.

We keep the admin module ignorant of the identity-service's storage layer.
All cross-service admin operations (list/terminate sessions, manage failed
logins, view+update rate-limit policy, list users, change roles) go through
this client. Cookies on the inbound admin request are forwarded so the
identity-service can apply its own admin role check.

If the identity-service is offline or returns 5xx, callers get a clean
IdentityServiceError; routers translate that into a 502 with a hint.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx
from fastapi import Request

from app.config import settings

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class IdentityServiceError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"identity-service {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _base_url() -> str:
    return settings.identity_service_url.rstrip("/")


def _cookies_from(request: Request) -> dict[str, str]:
    """Forward only the session cookie — keep the surface area minimal."""
    out: dict[str, str] = {}
    sess = request.cookies.get("applyd_session")
    if sess:
        out["applyd_session"] = sess
    csrf = request.cookies.get("applyd_csrf")
    if csrf:
        out["applyd_csrf"] = csrf
    return out


async def _call(
    request: Request,
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
) -> Any:
    url = f"{_base_url()}{path}"
    cookies = _cookies_from(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, params=params, data=data, cookies=cookies)
    except httpx.HTTPError as exc:
        log.warning("identity-service unreachable %s %s: %s", method, path, exc)
        raise IdentityServiceError(502, f"identity-service unreachable: {exc}") from exc
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = payload.get("detail") if isinstance(payload, dict) else None
        except Exception:
            detail = None
        raise IdentityServiceError(resp.status_code, str(detail or resp.text[:200]))
    if not resp.content:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text


async def list_sessions(request: Request) -> list[dict]:
    payload = await _call(request, "GET", "/api/admin/sessions")
    return list(payload or [])


async def terminate_session(request: Request, public_id: str, csrf_token: str) -> dict:
    return await _call(
        request,
        "POST",
        f"/api/admin/sessions/{public_id}/terminate",
        data={"csrf_token": csrf_token},
    )


async def list_failed_logins(request: Request, *, limit: int = 100) -> list[dict]:
    payload = await _call(request, "GET", "/api/admin/failed-logins", params={"limit": limit})
    return list(payload or [])


async def clear_failed_logins(
    request: Request,
    csrf_token: str,
    *,
    email: str | None = None,
    ip_address: str | None = None,
) -> dict:
    data: dict[str, Any] = {"csrf_token": csrf_token}
    if email:
        data["email"] = email
    if ip_address:
        data["ip_address"] = ip_address
    return await _call(request, "POST", "/api/admin/failed-logins/clear", data=data)


async def list_rate_limits(request: Request) -> dict:
    return await _call(request, "GET", "/api/admin/rate-limits") or {}


async def update_rate_limit_policy(
    request: Request,
    csrf_token: str,
    *,
    pair_max: int,
    email_max: int,
    ip_max: int,
    window_seconds: int,
    lockout_seconds: int,
) -> dict:
    return await _call(
        request,
        "POST",
        "/api/admin/rate-limits/policy",
        data={
            "csrf_token": csrf_token,
            "pair_max": pair_max,
            "email_max": email_max,
            "ip_max": ip_max,
            "window_seconds": window_seconds,
            "lockout_seconds": lockout_seconds,
        },
    )


async def unlock_rate_limit_bucket(
    request: Request,
    csrf_token: str,
    bucket_key: str,
) -> dict:
    return await _call(
        request,
        "POST",
        "/api/admin/rate-limits/unlock",
        data={"csrf_token": csrf_token, "bucket_key": bucket_key},
    )
