"""Operations + observability surface — drives the /settings page."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import settings
from app.database import get_db

router = APIRouter()


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
    # storage
    db_path: str
    db_size_bytes: int
    cache_dir: str
    cache_size_bytes: int
    total_jobs: int
    total_saved: int
    # config
    rolling_window_days: int
    ingest_hour_utc: int
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


@router.get("/", response_model=SettingsInfo)
def info() -> SettingsInfo:
    db_path = str(settings.db_path)
    cache_dir = str(settings.cache_dir)
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    cache_size = _dir_size(cache_dir)

    with get_db() as conn:
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        total_saved = conn.execute("SELECT COUNT(*) FROM saved_jobs").fetchone()[0]

    return SettingsInfo(
        db_path=db_path,
        db_size_bytes=db_size,
        cache_dir=cache_dir,
        cache_size_bytes=cache_size,
        total_jobs=total_jobs,
        total_saved=total_saved,
        rolling_window_days=settings.rolling_window_days,
        ingest_hour_utc=settings.ingest_hour_utc,
        manifest_url=settings.manifest_url,
        download_concurrency=settings.download_concurrency,
        debug=settings.debug,
    )


@router.get("/by_ats", response_model=list[AtsCount])
def by_ats_full() -> list[AtsCount]:
    """Per-ATS row counts — all sources, no limit, no time filter (raw DB state)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ats_type, COUNT(*) AS n FROM jobs "
            "GROUP BY ats_type ORDER BY n DESC"
        ).fetchall()
    return [AtsCount(ats_type=r["ats_type"], count=r["n"]) for r in rows]


@router.get("/ingest_log", response_model=list[IngestLogRow])
def ingest_log(
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
