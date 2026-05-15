from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path
from sqlite3 import IntegrityError
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.identity.auth import (
    RateLimitPolicy,
    admin_clear_failed_logins,
    admin_list_failed_logins,
    admin_list_locked_buckets,
    admin_list_sessions,
    admin_list_users,
    admin_set_user_role,
    admin_terminate_session,
    admin_unlock_bucket,
    authenticate_user,
    clear_all_sessions,
    clear_session,
    clear_signin_failures,
    create_session,
    create_user,
    get_rate_limit_policy,
    get_user_email,
    get_user_role,
    is_signin_rate_limited,
    is_signup_rate_limited,
    list_active_sessions,
    log_auth_event,
    record_signin_failure,
    record_signup_attempt,
    require_admin,
    set_rate_limit_policy,
    validate_password_strength,
    validate_session,
)

logger = logging.getLogger(__name__)
router = APIRouter()
_templates_dir = Path(__file__).resolve().parents[2] / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

_DASHBOARD_URL = "http://localhost:8000/dashboard"
_AUTH_ERROR_MESSAGES = {
    "invalid_credentials": "That email and password didn't match. Try again.",
    "weak_password": "Choose a stronger password (10+ chars, upper/lower/number/symbol).",
    "email_exists": "An account with that email already exists. Sign in instead?",
    "invalid_request": "The request could not be validated. Please try again.",
    "rate_limited": "Too many sign-in attempts. Please wait a few minutes and try again.",
    "signup_rate_limited": "Too many signup attempts from this network. Please try again later.",
}

def _allowed_redirect_hosts() -> set[str]:
    return {h.strip().lower() for h in settings.redirect_allow_hosts.split(",") if h.strip()}


def _default_dashboard_url(request: Request | None = None) -> str:
    if request is None:
        return _DASHBOARD_URL
    host = request.url.hostname or "localhost"
    scheme = request.url.scheme or "http"
    return f"{scheme}://{host}:8000/dashboard"


def _sanitize_next_url(raw_next: str | None, request: Request | None = None) -> str:
    default_url = _default_dashboard_url(request)
    if not raw_next:
        return default_url
    candidate = raw_next.strip()
    if any(ch in candidate for ch in ("\r", "\n", "\\")):
        return default_url
    parsed = urlsplit(candidate)
    if not parsed.netloc:
        if candidate.startswith("/") and not candidate.startswith("//"):
            return candidate
        return default_url
    if parsed.scheme not in {"http", "https"}:
        return default_url
    if parsed.netloc.lower() not in _allowed_redirect_hosts():
        return default_url
    return candidate


def _client_ip(request: Request) -> str:
    direct = request.client.host if request.client and request.client.host else "unknown"
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        return direct
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < hops:
        return direct
    return parts[-hops]


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _set_session_cookie(response: RedirectResponse, token: str, expires_at) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        expires=expires_at.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        max_age=settings.session_cookie_max_age_seconds,
        domain=settings.session_cookie_domain,
        path="/",
    )


def _csrf_token_from_request(request: Request) -> str | None:
    existing = (request.cookies.get(settings.csrf_cookie_name) or "").strip()
    if 16 <= len(existing) <= 128 and all(c.isalnum() or c in "-_" for c in existing):
        return existing
    return None


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        httponly=False,
        secure=settings.csrf_cookie_secure,
        samesite=settings.csrf_cookie_samesite,
        path="/",
    )


def _csrf_valid(request: Request, submitted_token: str) -> bool:
    cookie_token = request.cookies.get(settings.csrf_cookie_name, "")
    if not cookie_token or not submitted_token:
        return False
    return hmac.compare_digest(cookie_token, submitted_token)


def _delete_csrf_cookie(response: Response) -> None:
    response.delete_cookie(settings.csrf_cookie_name, path="/")


def require_admin_session(request: Request) -> int:
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="authentication required")
    require_admin(user_id)
    return user_id


def verify_request_user(request: Request) -> dict | None:
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        return None
    return {
        "authenticated": True,
        "user_id": user_id,
        "email": get_user_email(user_id),
        "role": get_user_role(user_id) or "user",
    }


@router.get("/signin")
def signin_page(request: Request):
    next_url = _sanitize_next_url(request.query_params.get("next"), request)
    error_code = request.query_params.get("error")
    csrf_token = _csrf_token_from_request(request) or secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "signin.html",
        {
            "next_url": next_url,
            "active_page": "signin",
            "error_message": _AUTH_ERROR_MESSAGES.get(error_code),
            "csrf_token": csrf_token,
        },
    )
    if not _csrf_token_from_request(request):
        _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/signin")
def signin_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
    csrf_token: str = Form(""),
):
    ip = _client_ip(request)
    ua = _user_agent(request)
    if not _csrf_valid(request, csrf_token):
        log_auth_event(event_type="signin", success=False, email=email, ip_address=ip, user_agent=ua, detail="csrf_failed")
        return RedirectResponse(url="/signin?error=invalid_request", status_code=303)
    if is_signin_rate_limited(ip, email):
        log_auth_event(event_type="signin", success=False, email=email, ip_address=ip, user_agent=ua, detail="rate_limited")
        return RedirectResponse(url="/signin?error=rate_limited", status_code=303)
    user_id = authenticate_user(email=email, password=password)
    if user_id is None:
        record_signin_failure(ip, email)
        log_auth_event(event_type="signin", success=False, email=email, ip_address=ip, user_agent=ua, detail="invalid_credentials")
        return RedirectResponse(url="/signin?error=invalid_credentials", status_code=303)

    clear_signin_failures(ip, email)
    stale_token = request.cookies.get(settings.session_cookie_name)
    if stale_token:
        clear_session(stale_token)
    token, expires_at = create_session(user_id, ip_address=ip, user_agent=ua)
    log_auth_event(event_type="signin", success=True, email=email, user_id=user_id, ip_address=ip, user_agent=ua)
    response = RedirectResponse(url=_sanitize_next_url(next, request), status_code=303)
    _set_session_cookie(response, token, expires_at)
    return response


@router.get("/signup")
def signup_page(request: Request):
    next_url = _sanitize_next_url(request.query_params.get("next"), request)
    error_code = request.query_params.get("error")
    csrf_token = _csrf_token_from_request(request) or secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request,
        "signup.html",
        {
            "next_url": next_url,
            "active_page": "signup",
            "error_message": _AUTH_ERROR_MESSAGES.get(error_code),
            "csrf_token": csrf_token,
        },
    )
    if not _csrf_token_from_request(request):
        _set_csrf_cookie(response, csrf_token)
    return response


@router.post("/signup")
def signup_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
    csrf_token: str = Form(""),
):
    ip = _client_ip(request)
    ua = _user_agent(request)
    if not _csrf_valid(request, csrf_token):
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail="csrf_failed")
        return RedirectResponse(url="/signup?error=invalid_request", status_code=303)
    if is_signup_rate_limited(ip):
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail="rate_limited")
        return RedirectResponse(url="/signup?error=signup_rate_limited", status_code=303)
    record_signup_attempt(ip)
    password_issue = validate_password_strength(password)
    if password_issue is not None:
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail=f"weak_password:{password_issue}")
        return RedirectResponse(url="/signup?error=weak_password", status_code=303)
    try:
        user_id = create_user(name=name, email=email, password=password)
    except IntegrityError:
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail="email_exists")
        return RedirectResponse(url="/signup?error=email_exists", status_code=303)

    stale_token = request.cookies.get(settings.session_cookie_name)
    if stale_token:
        clear_session(stale_token)
    token, expires_at = create_session(user_id, ip_address=ip, user_agent=ua)
    log_auth_event(event_type="signup", success=True, email=email, user_id=user_id, ip_address=ip, user_agent=ua)
    response = RedirectResponse(url=_sanitize_next_url(next, request), status_code=303)
    _set_session_cookie(response, token, expires_at)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    ip = _client_ip(request)
    ua = _user_agent(request)
    if not _csrf_valid(request, csrf_token):
        log_auth_event(event_type="logout", success=False, ip_address=ip, user_agent=ua, detail="csrf_failed")
        return RedirectResponse(url="/signin?error=invalid_request", status_code=303)
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token) if token else None
    if token:
        clear_session(token)
    log_auth_event(event_type="logout", success=True, user_id=user_id, ip_address=ip, user_agent=ua)
    response = RedirectResponse(url="/signin", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/", domain=settings.session_cookie_domain)
    _delete_csrf_cookie(response)
    return response


@router.get("/api/auth/verify")
def verify(request: Request):
    verified = verify_request_user(request)
    if verified is None:
        return JSONResponse({"authenticated": False}, status_code=401)
    return verified


@router.get("/api/auth/sessions")
def auth_sessions(request: Request):
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return {"sessions": list_active_sessions(user_id)}


@router.post("/api/auth/sessions/revoke-all")
def revoke_all_sessions(request: Request, csrf_token: str = Form(""), password: str = Form("")):
    ip = _client_ip(request)
    ua = _user_agent(request)
    if not _csrf_valid(request, csrf_token):
        return JSONResponse({"detail": "invalid request"}, status_code=400)
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    email = get_user_email(user_id)
    if not email or authenticate_user(email=email, password=password) != user_id:
        log_auth_event(event_type="revoke_all_sessions", success=False, user_id=user_id, ip_address=ip, user_agent=ua, detail="password_required")
        return JSONResponse({"detail": "password required"}, status_code=403)
    revoked = clear_all_sessions(user_id=user_id, keep_token=token)
    log_auth_event(event_type="revoke_all_sessions", success=True, user_id=user_id, ip_address=ip, user_agent=ua, detail=f"revoked={revoked}")
    return {"revoked": revoked}

