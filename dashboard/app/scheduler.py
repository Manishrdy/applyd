"""APScheduler wiring: daily cron + stale-aware catch-up at startup."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import get_db
from app.services.ingestion import run_ingestion
from app.services.manifest import latest_manifest_log

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def _has_any_jobs() -> bool:
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return n > 0


def _last_ingest_utc_date() -> date | None:
    """UTC date of the most recent successful ingestion, or None if there
    isn't one yet. Reads manifest_log.fetched_at — set at the start of
    every ingestion cycle in run_ingestion()."""
    with get_db() as conn:
        row = latest_manifest_log(conn)
    if row is None or not row["fetched_at"]:
        return None
    try:
        # fetched_at is written as datetime.now(timezone.utc).isoformat()
        # so it always has a TZ marker; fromisoformat handles both T-sep
        # and the trailing "+00:00".
        return datetime.fromisoformat(row["fetched_at"]).astimezone(timezone.utc).date()
    except ValueError:
        log.warning("could not parse manifest_log.fetched_at=%r", row["fetched_at"])
        return None


async def _run_daily() -> None:
    try:
        result = await run_ingestion()
        log.info("daily ingestion: %s", result.get("status"))
    except Exception:
        log.exception("daily ingestion failed")


async def _run_if_stale() -> None:
    """Startup catch-up: ingest if we haven't fetched today's batch yet.

    Three cases:
      1. DB empty → fresh install, fetch the initial dataset.
      2. Last successful ingest was on an earlier UTC date than today →
         the system was off when today's cron fired (or hasn't fired yet
         and upstream may already have new data). Fetch; if upstream
         manifest hasn't actually updated, run_ingestion short-circuits
         to "skipped" via should_ingest().
      3. Last successful ingest is on today's UTC date → already current,
         skip silently.
    """
    if not _has_any_jobs():
        log.info("startup: DB empty, running initial ingestion")
        try:
            result = await run_ingestion()
            log.info("initial ingestion: %s", result.get("status"))
        except Exception:
            log.exception("initial ingestion failed")
        return

    last_date = _last_ingest_utc_date()
    today_utc = datetime.now(timezone.utc).date()
    if last_date is None or last_date < today_utc:
        log.info(
            "startup: last successful ingest was %s, today is %s — running catch-up",
            last_date, today_utc,
        )
        try:
            result = await run_ingestion()
            log.info("startup catch-up: %s", result.get("status"))
        except Exception:
            log.exception("startup catch-up ingestion failed")
    else:
        log.info("startup: already ingested today (%s), skipping", today_utc)


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
        _run_if_stale,
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
