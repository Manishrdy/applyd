"""APScheduler wiring: daily cron + run-once-if-empty at startup."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import get_db
from app.services.ingestion import run_ingestion

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _has_any_jobs() -> bool:
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return n > 0


async def _run_daily() -> None:
    try:
        result = await run_ingestion()
        log.info("daily ingestion: %s", result.get("status"))
    except Exception:
        log.exception("daily ingestion failed")


async def _run_if_empty() -> None:
    if _has_any_jobs():
        log.info("startup: DB already populated, skipping initial ingestion")
        return
    log.info("startup: DB empty, running initial ingestion")
    try:
        result = await run_ingestion()
        log.info("initial ingestion: %s", result.get("status"))
    except Exception:
        log.exception("initial ingestion failed")


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_daily,
        trigger=CronTrigger(hour=settings.ingest_hour_utc, minute=0),
        id="daily_ingestion",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    _scheduler.add_job(
        _run_if_empty,
        id="startup_ingestion",
        replace_existing=True,
    )
    _scheduler.start()
    log.info("scheduler started (daily cron at %02d:00 UTC)", settings.ingest_hour_utc)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
