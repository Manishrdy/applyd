"""FastAPI app entry point.

Phase 1 shipped health + manual ingest. Phase 2 adds the full API surface:
jobs (list/search/facets/companies/detail), saved (CRUD), stats.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import get_db, init_db
from app.routers import jobs as jobs_router
from app.routers import pages as pages_router
from app.routers import saved as saved_router
from app.routers import stats as stats_router
from app.scheduler import start_scheduler, stop_scheduler
from app.services.ingestion import run_ingestion

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
app.include_router(pages_router.router, tags=["pages"])


# ---- meta -----------------------------------------------------------------


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
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
