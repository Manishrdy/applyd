"""Admin settings endpoints + page.

Moves the former public `/settings` use-case under `/admin/settings` and
admin-gates all related JSON APIs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.config import settings
from app.database import db_reclaimable_bytes, get_db, last_vacuum_at, vacuum_db

api_router = APIRouter()
legacy_api_router = APIRouter()
page_router = APIRouter()
log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


class IngestLogRow(BaseModel):
    fetched_at: str
    manifest_updated_at: str | None = None
    status: str
    rows_ingested: int | None = None
    rows_pruned: int | None = None
    duration_seconds: float | None = None
    error: str | None = None


class AtsCount(BaseModel):
    ats_type: str | None
    count: int


class SettingsInfo(BaseModel):
    db_path: str
    db_size_bytes: int
    db_reclaimable_bytes: int
    db_last_vacuum_at: str | None
    cache_dir: str
    cache_size_bytes: int
    total_jobs: int
    total_saved: int
    rolling_window_days: int
    ingest_hour_utc: int
    ingest_poll_interval_minutes: int
    ingest_poll_end_hour_utc: int
    manifest_url: str
    download_concurrency: int
    debug: bool


def _dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@page_router.get("/admin/settings")
def admin_settings_page(request: Request, admin: AdminUser = Depends(require_admin_user)):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"admin": admin, "active_page": "settings"},
    )


@api_router.get("/settings/", response_model=SettingsInfo)
def info(admin: AdminUser = Depends(require_admin_user)) -> SettingsInfo:
    db_path = str(settings.db_path)
    cache_dir = str(settings.cache_dir)
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    cache_size = _dir_size(cache_dir)

    with get_db() as conn:
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        total_saved = conn.execute("SELECT COUNT(*) FROM saved_jobs").fetchone()[0]
        reclaimable = db_reclaimable_bytes(conn)
    last_vac = last_vacuum_at()

    return SettingsInfo(
        db_path=db_path,
        db_size_bytes=db_size,
        db_reclaimable_bytes=reclaimable,
        db_last_vacuum_at=last_vac.isoformat() if last_vac else None,
        cache_dir=cache_dir,
        cache_size_bytes=cache_size,
        total_jobs=total_jobs,
        total_saved=total_saved,
        rolling_window_days=settings.rolling_window_days,
        ingest_hour_utc=settings.ingest_hour_utc,
        ingest_poll_interval_minutes=settings.ingest_poll_interval_minutes,
        ingest_poll_end_hour_utc=settings.ingest_poll_end_hour_utc,
        manifest_url=settings.manifest_url,
        download_concurrency=settings.download_concurrency,
        debug=settings.debug,
    )


@api_router.get("/settings/by_ats", response_model=list[AtsCount])
def by_ats_full(admin: AdminUser = Depends(require_admin_user)) -> list[AtsCount]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ats_type, COUNT(*) AS n FROM jobs "
            "GROUP BY ats_type ORDER BY n DESC"
        ).fetchall()
    return [AtsCount(ats_type=r["ats_type"], count=r["n"]) for r in rows]


class VacuumResult(BaseModel):
    size_before_bytes: int
    size_after_bytes: int
    reclaimed_bytes: int
    free_pages_before: int
    free_pages_after: int
    duration_seconds: float
    last_vacuum_at: str


@api_router.post("/settings/vacuum", response_model=VacuumResult)
def vacuum(request: Request, admin: AdminUser = Depends(require_admin_user)) -> VacuumResult:
    try:
        result = vacuum_db()
    except Exception as e:
        log.exception("manual VACUUM failed")
        raise HTTPException(status_code=500, detail=str(e))
    audit.record(admin=admin, action="vacuum_db", detail=result, request=request)
    return VacuumResult(
        size_before_bytes=result["size_before_bytes"],
        size_after_bytes=result["size_after_bytes"],
        reclaimed_bytes=result["reclaimed_bytes"],
        free_pages_before=result["free_pages_before"],
        free_pages_after=result["free_pages_after"],
        duration_seconds=result["duration_seconds"],
        last_vacuum_at=result["last_vacuum_at"],
    )


@api_router.get("/settings/ingest_log", response_model=list[IngestLogRow])
def ingest_log(
    admin: AdminUser = Depends(require_admin_user),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IngestLogRow]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT fetched_at, manifest_updated_at, status, rows_ingested, "
            "rows_pruned, duration_seconds, error FROM manifest_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [IngestLogRow(**dict(r)) for r in rows]


@legacy_api_router.get("/", response_model=SettingsInfo)
def legacy_info(admin: AdminUser = Depends(require_admin_user)) -> SettingsInfo:
    return info(admin)


@legacy_api_router.get("/by_ats", response_model=list[AtsCount])
def legacy_by_ats(admin: AdminUser = Depends(require_admin_user)) -> list[AtsCount]:
    return by_ats_full(admin)


@legacy_api_router.post("/vacuum", response_model=VacuumResult)
def legacy_vacuum(request: Request, admin: AdminUser = Depends(require_admin_user)) -> VacuumResult:
    return vacuum(request, admin)


@legacy_api_router.get("/ingest_log", response_model=list[IngestLogRow])
def legacy_ingest_log(
    admin: AdminUser = Depends(require_admin_user),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IngestLogRow]:
    return ingest_log(admin, limit)
