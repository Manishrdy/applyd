"""In-process identity admin service shim.

Keeps the previous async interface used by admin routers, but routes calls to
local identity modules instead of remote HTTP.
"""

from __future__ import annotations

from fastapi import Request

from app.identity import auth


class IdentityServiceError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"identity {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _csrf_valid(request: Request, submitted_token: str) -> bool:
    cookie = request.cookies.get("applyd_csrf", "")
    return bool(cookie and submitted_token and cookie == submitted_token)


async def list_sessions(request: Request) -> list[dict]:
    return auth.admin_list_sessions(limit=200)


async def terminate_session(request: Request, public_id: str, csrf_token: str) -> dict:
    if not _csrf_valid(request, csrf_token):
        raise IdentityServiceError(400, "invalid request")
    ok = auth.admin_terminate_session(public_id)
    if not ok:
        raise IdentityServiceError(404, "session not found")
    return {"terminated": True, "public_id": public_id}


async def list_failed_logins(request: Request, *, limit: int = 100) -> list[dict]:
    return auth.admin_list_failed_logins(limit=limit)


async def clear_failed_logins(
    request: Request,
    csrf_token: str,
    *,
    email: str | None = None,
    ip_address: str | None = None,
) -> dict:
    if not _csrf_valid(request, csrf_token):
        raise IdentityServiceError(400, "invalid request")
    return auth.admin_clear_failed_logins(email=email, ip_address=ip_address)


async def list_rate_limits(request: Request) -> dict:
    policy = auth.get_rate_limit_policy()
    return {
        "policy": {
            "pair_max": policy.pair_max,
            "email_max": policy.email_max,
            "ip_max": policy.ip_max,
            "window_seconds": policy.window_seconds,
            "lockout_seconds": policy.lockout_seconds,
        },
        "locked_buckets": auth.admin_list_locked_buckets(),
    }


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
    admin_id = getattr(request.state, "user_id", None)
    if admin_id is None:
        raise IdentityServiceError(401, "authentication required")
    if not _csrf_valid(request, csrf_token):
        raise IdentityServiceError(400, "invalid request")
    if any(v < 1 for v in (pair_max, email_max, ip_max, window_seconds, lockout_seconds)):
        raise IdentityServiceError(400, "all values must be >= 1")
    policy = auth.RateLimitPolicy(
        pair_max=pair_max,
        email_max=email_max,
        ip_max=ip_max,
        window_seconds=window_seconds,
        lockout_seconds=lockout_seconds,
    )
    auth.set_rate_limit_policy(policy, updated_by=str(admin_id))
    return {"policy": policy.__dict__}


async def unlock_rate_limit_bucket(
    request: Request,
    csrf_token: str,
    bucket_key: str,
) -> dict:
    if not _csrf_valid(request, csrf_token):
        raise IdentityServiceError(400, "invalid request")
    ok = auth.admin_unlock_bucket(bucket_key)
    if not ok:
        raise IdentityServiceError(404, "bucket not found")
    return {"unlocked": True, "bucket_key": bucket_key}
