"""FastAPI app entry point.

Phase 1 shipped health + manual ingest. Phase 2 adds the full API surface:
jobs (list/search/facets/companies/detail), saved (CRUD), stats.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.database import cached_jobs_total, get_db, init_db
from app.logging_config import (
    configure_logging,
    log_http_request,
    log_http_response,
    request_payload,
    response_payload,
)
from app.routers import jobs as jobs_router
from app.routers import pages as pages_router
from app.routers import saved as saved_router
from app.routers import scrape as scrape_router
from app.routers import settings as settings_router
from app.routers import stats as stats_router
from app.scheduler import start_scheduler, stop_scheduler
from app.services.ingestion import run_ingestion
from app.services.scrape_runner import (
    cleanup_orphans as cleanup_scrape_orphans,
    cleanup_retention as cleanup_scrape_retention,
)

configure_logging(settings.log_level)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    reclaimed = cleanup_scrape_orphans()
    if reclaimed:
        log.warning("reclaimed %d orphaned scrape run(s) from previous process", reclaimed)
    cleanup_scrape_retention()
    start_scheduler()
    log.info("applyd dashboard ready")
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title="applyd dashboard",
    description="MS1 — job search + filter dashboard over the jobhive dataset.",
    version="0.2.0",
    lifespan=lifespan,
)

# Frontend ships from the same origin, but allow local-dev cross-origin tooling.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(jobs_router.router, prefix="/api/jobs", tags=["jobs"])
app.include_router(saved_router.router, prefix="/api/saved", tags=["saved"])
app.include_router(stats_router.router, prefix="/api/stats", tags=["stats"])
app.include_router(settings_router.router, prefix="/api/settings", tags=["settings"])
app.include_router(scrape_router.router, prefix="/api/scrape", tags=["scrape"])
app.include_router(pages_router.router, tags=["pages"])


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    started = time.perf_counter()
    req_payload = await request_payload(request)
    log_http_request(log, request, req_payload)

    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.exception(
            "request failed method=%s path=%s elapsed_ms=%.2f",
            request.method, request.url.path, elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - started) * 1000
    if hasattr(response, "body_iterator") and response.body_iterator is not None:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        res_payload = response_payload(response, body)
        response = Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
    else:
        res_payload = response_payload(response)

    log_http_response(log, request, response, elapsed_ms, res_payload)
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public_prefixes = (
        "/static/",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/api/health",
    )
    protected_prefixes = (
        "/dashboard",
        "/saved",
        "/stats",
        "/settings",
        "/scrape",
        "/job/",
        "/api/",
    )
    if path == "/api/health":
        return await call_next(request)
    is_public = path == "/" or path.startswith(public_prefixes)
    if is_public:
        return await call_next(request)
    if path.startswith(protected_prefixes):
        token = request.cookies.get("applyd_session")
        authenticated = False
        if token:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    verify = await client.get(
                        f"{settings.identity_service_url}/api/auth/verify",
                        cookies={"applyd_session": token},
                    )
                authenticated = verify.status_code == 200
            except Exception:
                authenticated = False
        if not authenticated:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            target = str(request.url)
            signin_url = f"{settings.identity_service_url}/signin?next={quote(target, safe='')}"
            return RedirectResponse(url=signin_url, status_code=303)
    return await call_next(request)


@app.middleware("http")
async def bind_identity_service_url(request: Request, call_next):
    request.state.identity_service_url = settings.identity_service_url.rstrip("/")
    return await call_next(request)


# ---- error handlers ------------------------------------------------------
# Themed 404/500 for HTML routes; JSON keeps the FastAPI default for /api/*.

_error_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def _is_api_request(request: Request) -> bool:
    return request.url.path.startswith("/api/")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    level = logging.WARNING if exc.status_code < 500 else logging.ERROR
    log.log(
        level,
        "http exception path=%s status=%s detail=%s",
        request.url.path,
        exc.status_code,
        exc.detail,
    )
    if _is_api_request(request) or exc.status_code not in (404, 500):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    template = "errors/404.html" if exc.status_code == 404 else "errors/500.html"
    return _error_templates.TemplateResponse(
        request, template, {"transparency": None}, status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    if _is_api_request(request):
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    return _error_templates.TemplateResponse(
        request, "errors/500.html", {"transparency": None}, status_code=500,
    )


# ---- meta -----------------------------------------------------------------


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    total = cached_jobs_total()
    with get_db() as conn:
        last = conn.execute(
            "SELECT fetched_at, status, manifest_updated_at "
            "FROM manifest_log WHERE status='success' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "status": "ok",
        "total_jobs": total,
        "last_ingest": dict(last) if last else None,
        "rolling_window_days": settings.rolling_window_days,
    }


@app.post("/api/ingest", tags=["meta"])
async def ingest_now(force: bool = False) -> dict:
    """Manually trigger an ingestion cycle. Long-running — runs inline."""
    try:
        return await run_ingestion(force=force)
    except Exception as e:
        log.exception("manual ingestion failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ingest/status", tags=["meta"])
def ingest_status(limit: int = 10) -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT fetched_at, manifest_updated_at, status, "
            "rows_ingested, rows_pruned, duration_seconds, error "
            "FROM manifest_log ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    return {"recent": [dict(r) for r in rows]}
