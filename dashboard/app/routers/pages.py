"""HTML page routes — Jinja2 server-rendered.

Phase 3 ships a placeholder index + the styleguide. Phase 4 swaps index for
the unified search-first dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import cached_jobs_total, get_db
from app.identity.auth import require_user
from app.identity.routes import verify_request_user
from app.services import query as q

router = APIRouter()

_templates_dir = Path(__file__).resolve().parents[2] / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _ago(iso_str: str | None) -> str:
    if not iso_str:
        return "never"
    try:
        # parse ISO with optional tz; treat naive as UTC
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "?"
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86_400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86_400}d ago"


def _transparency() -> dict:
    """Header pill data — total visible jobs, window, freshness."""
    total = cached_jobs_total()
    with get_db() as conn:
        last = conn.execute(
            "SELECT fetched_at FROM manifest_log "
            "WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "total": total,
        "window_label": f"last {settings.rolling_window_days}d",
        "refreshed_label": _ago(last["fetched_at"] if last else None),
    }


def _summary() -> dict | None:
    """Cheap summary for the index placeholder cards."""
    try:
        total = cached_jobs_total()
        with get_db() as conn:
            us_24h = conn.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE country='US' AND COALESCE(posted_at, first_seen_at) >= datetime('now', '-24 hours')"
            ).fetchone()[0]
            us_7d = conn.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE country='US' AND COALESCE(posted_at, first_seen_at) >= datetime('now', '-7 days')"
            ).fetchone()[0]
            ats_count = conn.execute(
                "SELECT COUNT(DISTINCT ats_type) FROM jobs WHERE ats_type IS NOT NULL"
            ).fetchone()[0]
        return {
            "total_jobs": total,
            "us_24h": us_24h,
            "us_7d": us_7d,
            "ats_count": ats_count,
        }
    except Exception:
        return None


@router.get("/", response_class=HTMLResponse)
def landing_root(request: Request):
    # Smart root: anonymous users land on marketing page; signed-in users
    # jump straight to the protected dashboard.
    if verify_request_user(request) is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "landing.html",
        {},
    )


@router.get("/dashboard", response_class=HTMLResponse)
def index(request: Request):
    """Phase 4 unified search-first dashboard.

    Server-side render is intentionally minimal (just the chrome + transparency
    pill). The Alpine `dashboard()` component fetches /api/jobs/ and
    /api/jobs/facets on mount.
    """
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"transparency": _transparency()},
    )


@router.get("/placeholder", response_class=HTMLResponse, include_in_schema=False)
def placeholder(request: Request):
    """The Phase 3 placeholder page — kept for visual reference."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"transparency": _transparency(), "summary": _summary()},
    )


@router.get("/styleguide", response_class=HTMLResponse)
def styleguide(request: Request):
    return templates.TemplateResponse(
        request,
        "styleguide.html",
        {"transparency": _transparency()},
    )


@router.get("/job/{job_id}", response_class=HTMLResponse)
def job_detail(
    request: Request,
    job_id: int,
    user_id: int = Depends(require_user),
):
    """Server-render the detail view for a single job. Description is plain
    text (HTML stripped at ingest, see [[project-data-decisions]]) so we can
    safely render with `whitespace: pre-wrap` — no DOMPurify needed.
    """
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {q.DETAIL_COLUMNS} FROM jobs j WHERE j.id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        is_saved = bool(
            conn.execute(
                "SELECT 1 FROM saved_jobs WHERE user_id = ? AND job_id = ?",
                (user_id, job_id),
            ).fetchone()
        )

    job = q.row_to_detail(row, saved_ids={job_id} if is_saved else set())
    posted = job.get("posted_at") or job.get("first_seen_at")
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "transparency": _transparency(),
            "job": job,
            "time_label": _ago(posted),
        },
    )


@router.get("/saved", response_class=HTMLResponse)
def saved(request: Request):
    return templates.TemplateResponse(
        request,
        "saved.html",
        {"transparency": _transparency()},
    )


@router.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return templates.TemplateResponse(
        request,
        "stats.html",
        {"transparency": _transparency()},
    )


@router.get("/scrape", response_class=HTMLResponse)
def scrape(request: Request):
    """Manual local-scraper console — invoke the vendored jobhive scrapers
    and watch them progress live. Distinct from the daily jobhive cron path."""
    if not settings.local_scraper_enabled:
        raise HTTPException(404, "local scraper disabled")
    return templates.TemplateResponse(
        request,
        "scrape.html",
        {"transparency": _transparency()},
    )


@router.get("/scrape/runs/{run_id}", response_class=HTMLResponse)
def scrape_run_detail(request: Request, run_id: int):
    """360° view of one scrape run — all per-ATS counters, errors, and
    subprocess logs. Alpine fetches the full detail JSON on mount."""
    if not settings.local_scraper_enabled:
        raise HTTPException(404, "local scraper disabled")
    return templates.TemplateResponse(
        request,
        "scrape_detail.html",
        {"transparency": _transparency(), "run_id": run_id},
    )

