from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from sqlite3 import IntegrityError
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import hmac
import secrets

from app.auth import (
    authenticate_user,
    clear_all_sessions,
    clear_signin_failures,
    clear_session,
    create_session,
    create_user,
    get_user_email,
    is_signin_rate_limited,
    is_signup_rate_limited,
    list_active_sessions,
    log_auth_event,
    record_signin_failure,
    record_signup_attempt,
    validate_password_strength,
    validate_session,
)
from app.config import settings
from app.database import init_db


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("identity_service.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("identity-service started log_level=%s db=%s", settings.log_level, settings.db_path)
    yield


app = FastAPI(title="applyd identity-service", version="0.1.0", lifespan=lifespan)

_templates_dir = Path(__file__).resolve().parents[1] / "templates"
_static_dir = Path(__file__).resolve().parents[1] / "static"
templates = Jinja2Templates(directory=str(_templates_dir))
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


_DASHBOARD_URL = "http://localhost:8000/dashboard"

_AUTH_ERROR_MESSAGES = {
    "invalid_credentials": "That email and password didn't match. Try again.",
    "weak_password": "Choose a stronger password (10+ chars, upper/lower/number/symbol).",
    "email_exists": "An account with that email already exists. Sign in instead?",
    "invalid_request": "The request could not be validated. Please try again.",
    "rate_limited": "Too many sign-in attempts. Please wait a few minutes and try again.",
    "signup_rate_limited": "Too many signup attempts from this network. Please try again later.",
}


# ---------------------------------------------------------------------------
# Security headers middleware — sets a per-request CSP nonce that templates
# pick up via {{ request.state.csp_nonce }}, plus the standard hardening
# headers. CSP allows ONLY 'self' + same-nonce inline scripts/styles, which
# blocks reflected/stored XSS payloads from executing.
# ---------------------------------------------------------------------------


_BASE_CSP_PARTS = (
    "default-src 'self'",
    "img-src 'self' data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    # Styles: pragmatic 'unsafe-inline'. The landing page uses many inline
    # style="..." attributes, which CSP nonces cannot cover (only <style>
    # blocks can be nonced). Scripts — by far the more dangerous XSS
    # vector — remain strictly nonce-gated below.
    "style-src 'self' 'unsafe-inline'",
)


def _form_action_csp() -> str:
    """Allow form posts/redirects to land on the dashboard origin too.

    CSP's form-action directive is checked against *every* redirect hop, so
    a bare 'self' would block the cross-port 303 from /signin (:8100) to
    /dashboard (:8000) and the browser would silently keep the user on
    /signin. Mirror the same allowlist that sanitises the `next` parameter.
    """
    scheme = "https" if settings.session_cookie_secure else "http"
    hosts = sorted(_allowed_redirect_hosts())
    sources = ["'self'"] + [f"{scheme}://{h}" for h in hosts]
    return "form-action " + " ".join(sources)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    # New nonce per request; templates inject this on inline <script> blocks.
    request.state.csp_nonce = secrets.token_urlsafe(16)
    response: Response = await call_next(request)
    nonce = getattr(request.state, "csp_nonce", "")
    csp = "; ".join(_BASE_CSP_PARTS + (
        _form_action_csp(),
        f"script-src 'self' 'nonce-{nonce}'",
    ))
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "interest-cohort=(), browsing-topics=(), geolocation=(), microphone=(), camera=()",
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    # HSTS only makes sense once we know the user reached us over TLS. Tying
    # it to the secure-cookie flag is a reasonable proxy for "production".
    if settings.session_cookie_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


# ---------------------------------------------------------------------------
# Redirect / IP / UA helpers
# ---------------------------------------------------------------------------


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
    # Defend against CRLF injection and protocol-relative tricks like "/\evil".
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
    """Resolve the client IP, honouring X-Forwarded-For only when trusted.

    `trusted_proxy_hops` is the number of reverse proxies known to sit in
    front of this service that always append the previous hop to XFF. With
    that count N, the real client is at parts[-N] — the leftmost entry that
    one of our trusted proxies wrote.

    If XFF has fewer than N entries, the request is malformed (or did not
    actually traverse all expected proxies) and we fall back to the direct
    socket peer rather than trusting attacker-controllable input.
    """
    direct = request.client.host if request.client and request.client.host else "unknown"
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        return direct
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < hops:
        # Fewer entries than expected proxies → don't trust any of it.
        return direct
    return parts[-hops]


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


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
    """Return a validated CSRF token from cookies, if present."""
    existing = (request.cookies.get(settings.csrf_cookie_name) or "").strip()
    # Accept any non-empty, plausible token. token_urlsafe(32) => ~43 chars,
    # but enforce a generous floor and a hard ceiling to keep things sane.
    if 16 <= len(existing) <= 128 and all(
        c.isalnum() or c in "-_" for c in existing
    ):
        return existing
    return None


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        httponly=False,  # the form template echoes this into a hidden field
        secure=settings.csrf_cookie_secure,
        samesite=settings.csrf_cookie_samesite,
        path="/",
    )


def _csrf_valid(request: Request, submitted_token: str) -> bool:
    cookie_token = request.cookies.get(settings.csrf_cookie_name, "")
    if not cookie_token or not submitted_token:
        logger.warning(
            "csrf check failed: cookie_present=%s token_present=%s path=%s",
            bool(cookie_token),
            bool(submitted_token),
            request.url.path,
        )
        return False
    if not hmac.compare_digest(cookie_token, submitted_token):
        logger.warning("csrf check failed: cookie/token mismatch path=%s", request.url.path)
        return False
    return True


def _delete_csrf_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def landing(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"dashboard_url": _DASHBOARD_URL, "active_page": "landing"},
    )


@app.get("/landing")
def landing_alias(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"dashboard_url": _DASHBOARD_URL, "active_page": "landing"},
    )


@app.get("/signin")
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


@app.post("/signin")
def signin_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/dashboard"),
    csrf_token: str = Form(""),
):
    ip = _client_ip(request)
    ua = _user_agent(request)
    logger.info("POST /signin email=%s ip=%s", email, ip)

    if not _csrf_valid(request, csrf_token):
        logger.warning("signin rejected: csrf_failed email=%s ip=%s", email, ip)
        log_auth_event(event_type="signin", success=False, email=email, ip_address=ip, user_agent=ua, detail="csrf_failed")
        return RedirectResponse(url="/signin?error=invalid_request", status_code=303)

    if is_signin_rate_limited(ip, email):
        logger.warning("signin rejected: rate_limited email=%s ip=%s", email, ip)
        log_auth_event(event_type="signin", success=False, email=email, ip_address=ip, user_agent=ua, detail="rate_limited")
        return RedirectResponse(url="/signin?error=rate_limited", status_code=303)

    user_id = authenticate_user(email=email, password=password)
    if user_id is None:
        logger.info("signin rejected: invalid_credentials email=%s ip=%s", email, ip)
        record_signin_failure(ip, email)
        log_auth_event(event_type="signin", success=False, email=email, ip_address=ip, user_agent=ua, detail="invalid_credentials")
        return RedirectResponse(url="/signin?error=invalid_credentials", status_code=303)

    clear_signin_failures(ip, email)
    logger.info("signin ok user_id=%s email=%s", user_id, email)

    # Session-fixation hardening: if the user arrives with a stale session
    # cookie (e.g. signing in on a shared machine, or re-authenticating
    # after a privilege drop), invalidate it server-side before issuing the
    # new one. The new cookie immediately overwrites the value on the wire.
    stale_token = request.cookies.get(settings.session_cookie_name)
    if stale_token:
        clear_session(stale_token)

    token, expires_at = create_session(user_id, ip_address=ip, user_agent=ua)
    log_auth_event(event_type="signin", success=True, email=email, user_id=user_id, ip_address=ip, user_agent=ua)
    response = RedirectResponse(url=_sanitize_next_url(next, request), status_code=303)
    _set_session_cookie(response, token, expires_at)
    return response


@app.get("/signup")
def signup_page(request: Request):
    next_url = _sanitize_next_url(request.query_params.get("next"), request)
    error_code = request.query_params.get("error")
    csrf_token = _csrf_token_from_request(request) or secrets.token_urlsafe(32)
    logger.debug(
        "GET /signup csrf_cookie_present=%s error=%s next=%s",
        bool(request.cookies.get(settings.csrf_cookie_name)),
        error_code,
        next_url,
    )
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


@app.post("/signup")
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
    logger.info("POST /signup email=%s ip=%s", email, ip)

    if not _csrf_valid(request, csrf_token):
        logger.warning("signup rejected: csrf_failed email=%s ip=%s", email, ip)
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail="csrf_failed")
        return RedirectResponse(url="/signup?error=invalid_request", status_code=303)

    # Stop signup mills and email-enumeration probes at the network edge.
    if is_signup_rate_limited(ip):
        logger.warning("signup rejected: rate_limited ip=%s email=%s", ip, email)
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail="rate_limited")
        return RedirectResponse(url="/signup?error=signup_rate_limited", status_code=303)
    record_signup_attempt(ip)

    password_issue = validate_password_strength(password)
    if password_issue is not None:
        logger.info("signup rejected: weak_password (%s) email=%s", password_issue, email)
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail=f"weak_password:{password_issue}")
        return RedirectResponse(url="/signup?error=weak_password", status_code=303)
    try:
        user_id = create_user(name=name, email=email, password=password)
    except IntegrityError:
        logger.info("signup rejected: email_exists email=%s", email)
        log_auth_event(event_type="signup", success=False, email=email, ip_address=ip, user_agent=ua, detail="email_exists")
        return RedirectResponse(url="/signup?error=email_exists", status_code=303)
    except Exception:
        logger.exception("signup failed: unexpected error email=%s ip=%s", email, ip)
        raise

    # Same fixation defence as signin — wipe any pre-existing session
    # cookie's row before minting the new one.
    stale_token = request.cookies.get(settings.session_cookie_name)
    if stale_token:
        clear_session(stale_token)

    token, expires_at = create_session(user_id, ip_address=ip, user_agent=ua)
    logger.info("signup ok user_id=%s email=%s", user_id, email)
    log_auth_event(event_type="signup", success=True, email=email, user_id=user_id, ip_address=ip, user_agent=ua)
    response = RedirectResponse(url=_sanitize_next_url(next, request), status_code=303)
    _set_session_cookie(response, token, expires_at)
    return response


@app.post("/logout")
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
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        domain=settings.session_cookie_domain,
    )
    # Drop the CSRF cookie too — the token is no longer bound to anything
    # meaningful, and leaving it lying around just clutters the cookie jar.
    _delete_csrf_cookie(response)
    return response


@app.get("/api/auth/verify")
def verify(request: Request):
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        return JSONResponse({"authenticated": False}, status_code=401)
    return {"authenticated": True, "user_id": user_id}


@app.get("/api/auth/sessions")
def auth_sessions(request: Request):
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return {"sessions": list_active_sessions(user_id)}


@app.post("/api/auth/sessions/revoke-all")
def revoke_all_sessions(
    request: Request,
    csrf_token: str = Form(""),
    password: str = Form(""),
):
    """Revoke every other session for the signed-in user.

    Requires both a valid CSRF token AND a re-entered password. The password
    re-prompt blocks a stolen session from locking out the real owner: an
    attacker holding only the cookie cannot empty out the legitimate
    user's other sessions without also proving they know the password.
    """
    ip = _client_ip(request)
    ua = _user_agent(request)
    if not _csrf_valid(request, csrf_token):
        return JSONResponse({"detail": "invalid request"}, status_code=400)
    token = request.cookies.get(settings.session_cookie_name)
    user_id = validate_session(token)
    if user_id is None:
        return JSONResponse({"detail": "authentication required"}, status_code=401)

    email = get_user_email(user_id)
    if not email:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    if authenticate_user(email=email, password=password) != user_id:
        log_auth_event(
            event_type="revoke_all_sessions",
            success=False,
            user_id=user_id,
            ip_address=ip,
            user_agent=ua,
            detail="password_required",
        )
        return JSONResponse({"detail": "password required"}, status_code=403)

    revoked = clear_all_sessions(user_id=user_id, keep_token=token)
    log_auth_event(
        event_type="revoke_all_sessions",
        success=True,
        user_id=user_id,
        ip_address=ip,
        user_agent=ua,
        detail=f"revoked={revoked}",
    )
    return {"revoked": revoked}


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "identity-service"}
