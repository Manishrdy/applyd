"""APScheduler wiring: daily cron + stale-aware catch-up at startup."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.database import get_db
from app.services.ingestion import run_ingestion
from app.services.manifest import latest_manifest_log

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_ingest_lock: asyncio.Lock | None = None


def _utc_day_bounds_iso(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = datetime.fromtimestamp(start.timestamp() + 86_400, tz=timezone.utc)
    return start.isoformat(), end.isoformat()


def _get_ingest_lock() -> asyncio.Lock:
    global _ingest_lock
    if _ingest_lock is None:
        _ingest_lock = asyncio.Lock()
    return _ingest_lock


def _window_start_iso(day: date) -> str:
    return datetime(
        day.year, day.month, day.day, settings.ingest_hour_utc, 0, 0, tzinfo=timezone.utc
    ).isoformat()


def _should_run_catchup_poll_now(now_utc: datetime) -> bool:
    """True only when:
    - we're inside the configured poll window,
    - and today's 11:00+ ingestion history contains no success yet,
    - and latest 11:00+ attempt is skipped (manifest unchanged).
    """
    if settings.ingest_poll_interval_minutes <= 0:
        return False
    if now_utc.hour < settings.ingest_hour_utc:
        return False
    if now_utc.hour > settings.ingest_poll_end_hour_utc:
        return False

    day = now_utc.date()
    day_start_iso, day_end_iso = _utc_day_bounds_iso(day)
    window_start_iso = _window_start_iso(day)

    with get_db() as conn:
        any_success = conn.execute(
            "SELECT 1 FROM manifest_log "
            "WHERE fetched_at >= ? AND fetched_at < ? "
            "  AND fetched_at >= ? "
            "  AND status = 'success' "
            "LIMIT 1",
            (day_start_iso, day_end_iso, window_start_iso),
        ).fetchone()
        if any_success is not None:
            return False

        latest = conn.execute(
            "SELECT status FROM manifest_log "
            "WHERE fetched_at >= ? AND fetched_at < ? "
            "  AND fetched_at >= ? "
            "ORDER BY id DESC LIMIT 1",
            (day_start_iso, day_end_iso, window_start_iso),
        ).fetchone()

    if latest is None:
        # No 11:00+ attempt recorded yet today.
        return False
    return latest["status"] == "skipped"


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
        async with _get_ingest_lock():
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
            async with _get_ingest_lock():
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
            async with _get_ingest_lock():
                result = await run_ingestion()
            log.info("startup catch-up: %s", result.get("status"))
        except Exception:
            log.exception("startup catch-up ingestion failed")
    else:
        log.info("startup: already ingested today (%s), skipping", today_utc)


async def _run_catchup_poll() -> None:
    """Periodic post-11 UTC check.

    Runs only when today's 11:00+ ingestion has only produced skipped results
    so far (manifest unchanged) and no success yet.
    """
    now_utc = datetime.now(timezone.utc)
    if not _should_run_catchup_poll_now(now_utc):
        return

    lock = _get_ingest_lock()
    if lock.locked():
        log.info("catch-up poll: ingestion already running, skipping this tick")
        return

    try:
        async with lock:
            result = await run_ingestion()
        log.info("catch-up poll ingestion: %s", result.get("status"))
    except Exception:
        log.exception("catch-up poll ingestion failed")


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
    _scheduler.add_job(
        _run_catchup_poll,
        trigger=IntervalTrigger(minutes=settings.ingest_poll_interval_minutes),
        id="catchup_poll_ingestion",
        replace_existing=True,
        coalesce=True,
    )
    _scheduler.start()
    log.info(
        "scheduler started (daily cron at %02d:00 UTC; catch-up poll every %dm until %02d:59 UTC on skipped days)",
        settings.ingest_hour_utc,
        settings.ingest_poll_interval_minutes,
        settings.ingest_poll_end_hour_utc,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
