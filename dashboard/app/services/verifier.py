"""HTTP verifier: re-checks job URLs and decides active/expired/unknown.

Three callers feed the same verify_job():
  * scheduler.verify_suspected     — drains the suspected pool every N min
  * scheduler.verify_manifest_drops — fires after each successful ingest
  * scheduler.periodic_full_sweep   — covers active corpus every N days

All results flow through job_lifecycle.on_http_check, which writes
job_verification_log and may transition jobs.verification_status.

Per-ATS body-text matchers cover the top providers (where status alone is
unreliable — Ashby, iCIMS, Workday, etc. return 200 with expiry text in
the body). The dispatch falls back to HTTP-status-only for unknown ATSes
to avoid generic regex false-positives.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import httpx

from app.config import settings
from app.database import get_db
from app.services import job_lifecycle

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerificationResult:
    result: job_lifecycle.HttpResult   # active | expired | unknown | error
    http_status: int | None
    detector: str | None
    detail: str | None


# ---- Per-ATS body matchers ------------------------------------------------
#
# Each matcher is given the response body (lowercased) and returns one of:
#   'expired' — definitive: the page itself says the job is closed
#   'active'  — definitive: the page renders the normal job content
#   None      — inconclusive, caller falls back to HTTP status
#
# Patterns are intentionally narrow per-ATS. Generic body-text matching
# across all ATSes is too noisy (job *descriptions* sometimes contain
# phrases like "no longer accepting unsolicited resumes" that would
# false-positive a corporate-policy match).

def _has_any(body: str, phrases: Iterable[str]) -> bool:
    return any(p in body for p in phrases)


def match_greenhouse(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "this job is no longer available",
        "the job you were looking for could not be found",
        "we couldn't find that job",
    ]):
        return "expired"
    if "application" in body and "first name" in body:
        return "active"
    return None


def match_lever(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "this posting is no longer available",
        "we are no longer accepting applications",
        "page not found",
    ]):
        return "expired"
    if "lever-application" in body or "application-form" in body:
        return "active"
    return None


def match_ashby(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "this job is no longer available",
        "job is closed",
        "position is no longer accepting applications",
    ]):
        return "expired"
    return None


def match_workday(body: str, status: int) -> str | None:
    # Workday almost never 404s — it renders an error inside a SPA shell.
    if _has_any(body, [
        "this job posting is no longer available",
        "the job you have selected is no longer available",
        "data-automation-id=\"errormessage\"",
    ]):
        return "expired"
    return None


def match_icims(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "the position you are trying to view",
        "job has been filled",
        "this position is no longer available",
    ]):
        return "expired"
    return None


def match_workable(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "this job is no longer accepting applications",
        "this job has been filled",
    ]):
        return "expired"
    return None


def match_smartrecruiters(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "this job is no longer accepting applications",
        "position has been closed",
    ]):
        return "expired"
    return None


def match_bamboohr(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "we're sorry, this job is no longer available",
        "this job is no longer accepting applications",
    ]):
        return "expired"
    return None


def match_recruitee(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "this job is no longer available",
        "vacancy is no longer available",
    ]):
        return "expired"
    return None


def match_jazzhr(body: str, status: int) -> str | None:
    if status in (404, 410):
        return "expired"
    if _has_any(body, [
        "the job you are trying to view",
        "this position has been filled",
    ]):
        return "expired"
    return None


def match_generic(body: str, status: int) -> str | None:
    """Status-only fallback for unknown ATSes. No body regex by design —
    too noisy across the long tail. Definitive only on hard failures."""
    if status in (404, 410):
        return "expired"
    return None


MATCHERS: dict[str, Callable[[str, int], str | None]] = {
    "greenhouse": match_greenhouse,
    "lever": match_lever,
    "ashby": match_ashby,
    "workday": match_workday,
    "icims": match_icims,
    "workable": match_workable,
    "smartrecruiters": match_smartrecruiters,
    "bamboohr": match_bamboohr,
    "recruitee": match_recruitee,
    "jazzhr": match_jazzhr,
}


# ---- Per-host concurrency -------------------------------------------------

_host_locks: dict[str, asyncio.Semaphore] = {}
_global_sem: asyncio.Semaphore | None = None
_host_backoff: dict[str, float] = {}


def _host_semaphore(ats_type: str) -> asyncio.Semaphore:
    sem = _host_locks.get(ats_type)
    if sem is None:
        sem = asyncio.Semaphore(settings.verifier_per_host_concurrency)
        _host_locks[ats_type] = sem
    return sem


def _global_semaphore() -> asyncio.Semaphore:
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(settings.verifier_global_concurrency)
    return _global_sem


async def verify_job(
    client: httpx.AsyncClient,
    *,
    job_id: int,
    url: str,
    ats_type: str | None,
) -> VerificationResult:
    """Run one job's availability check. Doesn't write to DB — caller
    routes the result through job_lifecycle.on_http_check."""
    matcher = MATCHERS.get((ats_type or "").lower(), match_generic)
    detector_name = matcher.__name__

    host_key = (ats_type or "_unknown").lower()
    loop = asyncio.get_event_loop()
    if (skip_until := _host_backoff.get(host_key)) and loop.time() < skip_until:
        return VerificationResult("unknown", None, detector_name, "skipped per host backoff")

    async with _global_semaphore(), _host_semaphore(host_key):
        try:
            # HEAD first — many ATSes answer it cheaply. Some refuse with
            # 405 and we fall through to GET.
            r = await client.head(url, follow_redirects=True,
                                  timeout=settings.verifier_request_timeout_seconds)
            if r.status_code in (405, 501):
                r = await client.get(url, follow_redirects=True,
                                     timeout=settings.verifier_request_timeout_seconds)
            elif r.status_code in (200, 301, 302):
                # HEAD says 200 but the page might still be a 'no longer
                # available' shell. Do the GET to inspect the body.
                r = await client.get(url, follow_redirects=True,
                                     timeout=settings.verifier_request_timeout_seconds)
        except httpx.TimeoutException:
            return VerificationResult("error", None, detector_name, "timeout")
        except httpx.HTTPError as e:
            return VerificationResult("error", None, detector_name, f"http: {e}")
        except Exception as e:  # network/SSL/etc
            return VerificationResult("error", None, detector_name, f"error: {e}")

    status = r.status_code

    if status == 429:
        # Respect rate limit — back off this host for one cycle.
        _host_backoff[host_key] = loop.time() + 3600
        return VerificationResult("unknown", status, detector_name, "429 backoff")

    if status in (404, 410):
        return VerificationResult("expired", status, detector_name, f"HTTP {status}")

    body = ""
    try:
        body = (r.text or "").lower()[:200_000]  # cap body scan
    except Exception:
        body = ""

    # Listing-root redirect: an ATS that redirects a closed job to its
    # company-wide listings (greenhouse does this) is a strong signal.
    if str(r.url).rstrip("/").endswith(("/jobs", "/careers")):
        return VerificationResult(
            "expired", status, detector_name,
            f"redirected to listing root: {r.url}",
        )

    matched = matcher(body, status)
    if matched == "expired":
        return VerificationResult("expired", status, detector_name, "matcher: expired")
    if matched == "active":
        return VerificationResult("active", status, detector_name, "matcher: active")

    # Inconclusive: 200 OK with no signal — assume active by default
    # (we'd rather false-negative than wrongly hide a live job).
    if 200 <= status < 300:
        return VerificationResult("active", status, detector_name, "assumed active (200, no expiry signal)")
    return VerificationResult("unknown", status, detector_name, f"inconclusive HTTP {status}")


# ---- Drains ---------------------------------------------------------------


def _pick_suspected_jobs(conn, limit: int) -> list[tuple[int, str, str | None]]:
    rows = conn.execute(
        "SELECT id, url, ats_type FROM jobs "
        "WHERE verification_status = 'suspected' "
        "ORDER BY verification_status_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [(int(r["id"]), str(r["url"]), r["ats_type"]) for r in rows]


def _pick_manifest_drop_jobs(conn, limit: int) -> list[tuple[int, str, str | None, int]]:
    rows = conn.execute(
        "SELECT id, url, ats_type, missed_ingest_cycles FROM jobs "
        "WHERE missed_ingest_cycles >= 2 AND verification_status = 'active' "
        "ORDER BY missed_ingest_cycles DESC, id ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        (int(r["id"]), str(r["url"]), r["ats_type"], int(r["missed_ingest_cycles"]))
        for r in rows
    ]


def _pick_periodic_sweep_jobs(
    conn, limit: int, sweep_days: int, sweep_all: bool
) -> list[tuple[int, str, str | None]]:
    """Pick the next batch of active jobs to verify.

    Two modes:
      * sweep_all=True (default) — walk the entire active corpus
        continuously, oldest-checked first. Every job gets touched.
      * sweep_all=False          — only pick jobs older than
        verifier_sweep_days; useful when you want to back off after a
        full pass has completed.
    """
    if sweep_all:
        rows = conn.execute(
            "SELECT id, url, ats_type FROM jobs "
            "WHERE verification_status = 'active' "
            "ORDER BY last_verified_at IS NULL DESC, "  # never-checked first
            "         last_verified_at ASC, id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, url, ats_type FROM jobs "
            "WHERE verification_status = 'active' "
            "  AND (last_verified_at IS NULL "
            "       OR last_verified_at < datetime('now', ?)) "
            "ORDER BY last_verified_at IS NULL DESC, "
            "         last_verified_at ASC, id ASC "
            "LIMIT ?",
            (f"-{sweep_days} days", limit),
        ).fetchall()
    return [(int(r["id"]), str(r["url"]), r["ats_type"]) for r in rows]


async def _drain(
    jobs: list[tuple[int, str, str | None]],
    trigger: job_lifecycle.Signal,
    *,
    run_kind: str,
) -> dict:
    if not jobs:
        return {"checked": 0, "total": 0, "cancelled": False}
    results = {"checked": 0, "total": len(jobs), "active": 0, "expired": 0, "unknown": 0, "error": 0}
    run_id = _start_run(run_kind=run_kind, total=len(jobs))
    cancelled = False
    last_flush = 0.0
    async with httpx.AsyncClient(
        headers={"User-Agent": "applyd-verifier/1.0 (+https://applyd.app)"},
        timeout=settings.verifier_request_timeout_seconds,
        follow_redirects=True,
    ) as client:
        async def _one(job_id: int, url: str, ats_type: str | None) -> None:
            r = await verify_job(client, job_id=job_id, url=url, ats_type=ats_type)
            results["checked"] += 1
            results[r.result] = results.get(r.result, 0) + 1
            with get_db() as conn:
                job_lifecycle.on_http_check(
                    conn, job_id,
                    result=r.result, http_status=r.http_status,
                    detector=r.detector, detail=r.detail, trigger=trigger,
                )
        batch_size = max(1, settings.verifier_global_concurrency)
        for i in range(0, len(jobs), batch_size):
            if _cancel_requested(run_id):
                cancelled = True
                break
            batch = jobs[i:i + batch_size]
            await asyncio.gather(*[_one(j, u, a) for (j, u, a) in batch])
            now = time.monotonic()
            if now - last_flush >= 0.5:
                _update_run_progress(run_id, results)
                last_flush = now
    _update_run_progress(run_id, results)
    _finish_run(run_id, "cancelled" if cancelled else "completed", note="cancel requested" if cancelled else None)
    results["cancelled"] = cancelled
    return results


async def drain_suspected(batch_size: int | None = None) -> dict:
    """Verify a slice of currently-suspected jobs."""
    if not settings.expired_detection_enabled:
        return {"checked": 0, "skipped": "disabled"}
    limit = batch_size or settings.verifier_suspected_batch
    with get_db() as conn:
        jobs = _pick_suspected_jobs(conn, limit)
    return await _drain(jobs, trigger="http_check", run_kind="suspected")


async def drain_manifest_drops(batch_size: int = 200) -> dict:
    """Verify jobs that have missed >=2 successful ingest cycles."""
    if not settings.expired_detection_enabled:
        return {"checked": 0, "skipped": "disabled"}
    with get_db() as conn:
        rows = _pick_manifest_drop_jobs(conn, batch_size)
        # Apply the manifest-drop signal first (escalates active→suspected
        # without needing the network) so even verifier failures don't lose
        # the signal entirely.
        for job_id, _u, _a, missed in rows:
            job_lifecycle.on_manifest_drop(conn, job_id, missed)
    jobs = [(j, u, a) for (j, u, a, _m) in rows]
    return await _drain(jobs, trigger="manifest_drop", run_kind="manifest_drop")


def _auto_sweep_batch_size(corpus_size: int) -> int:
    days = max(1, settings.verifier_sweep_days)
    interval_min = max(1, settings.verifier_sweep_interval_minutes)
    # Number of ticks per `days` window. Each tick handles 1/ticks of corpus.
    ticks_per_window = (days * 24 * 60) // interval_min
    return max(1, corpus_size // max(1, ticks_per_window))


async def drain_periodic_sweep(batch_size: int | None = None) -> dict:
    """Verify a slice of active jobs."""
    if not settings.expired_detection_enabled:
        return {"checked": 0, "skipped": "disabled"}
    if batch_size is None:
        batch_size = settings.verifier_sweep_batch_size
    if batch_size is None:
        with get_db() as conn:
            active = int(conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE verification_status = 'active'"
            ).fetchone()[0])
        batch_size = _auto_sweep_batch_size(active)
    with get_db() as conn:
        jobs = _pick_periodic_sweep_jobs(
            conn, batch_size,
            settings.verifier_sweep_days,
            settings.verifier_sweep_all_active,
        )
    return await _drain(jobs, trigger="http_check", run_kind="periodic_sweep")


def _start_run(*, run_kind: str, total: int) -> int:
    with get_db() as conn:
        conn.execute(
            "UPDATE verifier_runs SET status='cancelled', finished_at=datetime('now'), note='interrupted by new process start' "
            "WHERE status IN ('running','cancel_requested')"
        )
        cur = conn.execute(
            "INSERT INTO verifier_runs (kind, status, total_jobs) VALUES (?, 'running', ?)",
            (run_kind, int(total)),
        )
        return int(cur.lastrowid)


def _update_run_progress(run_id: int, results: dict) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE verifier_runs SET "
            "checked_jobs=?, active_count=?, expired_count=?, unknown_count=?, error_count=? "
            "WHERE id=?",
            (
                int(results.get("checked", 0)),
                int(results.get("active", 0)),
                int(results.get("expired", 0)),
                int(results.get("unknown", 0)),
                int(results.get("error", 0)),
                int(run_id),
            ),
        )


def _finish_run(run_id: int, status: str, note: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE verifier_runs SET status=?, finished_at=datetime('now'), note=COALESCE(?, note) WHERE id=?",
            (status, note, int(run_id)),
        )


def _cancel_requested(run_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM verifier_runs WHERE id=?",
            (int(run_id),),
        ).fetchone()
    return row is not None and str(row["status"]) == "cancel_requested"


def request_cancel_active_run() -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE verifier_runs SET status='cancel_requested' "
            "WHERE status='running'"
        )
        return int(cur.rowcount or 0) > 0


def get_run_state() -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, kind, status, started_at, finished_at, total_jobs, checked_jobs, "
            "active_count, expired_count, unknown_count, error_count "
            "FROM verifier_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {
            "running": False,
            "id": None,
            "kind": None,
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "total_jobs": 0,
            "checked_jobs": 0,
            "progress_pct": 0.0,
            "rate_jobs_per_sec": 0.0,
            "eta_seconds": None,
        }
    total = int(row["total_jobs"] or 0)
    checked = int(row["checked_jobs"] or 0)
    started_at = str(row["started_at"]) if row["started_at"] else None
    finished_at = str(row["finished_at"]) if row["finished_at"] else None
    rate = 0.0
    eta = None
    if started_at:
        try:
            from datetime import datetime, timezone
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            ended = datetime.now(timezone.utc) if not finished_at else datetime.fromisoformat(finished_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            elapsed = max(0.001, (ended - started).total_seconds())
            rate = checked / elapsed
            if rate > 0 and total > checked and str(row["status"]) in ("running", "cancel_requested"):
                eta = int((total - checked) / rate)
        except Exception:
            rate = 0.0
            eta = None
    return {
        "running": str(row["status"]) in ("running", "cancel_requested"),
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "started_at": started_at,
        "finished_at": finished_at,
        "total_jobs": total,
        "checked_jobs": checked,
        "active": int(row["active_count"] or 0),
        "expired": int(row["expired_count"] or 0),
        "unknown": int(row["unknown_count"] or 0),
        "error": int(row["error_count"] or 0),
        "progress_pct": round((checked / total) * 100, 2) if total > 0 else 0.0,
        "rate_jobs_per_sec": round(rate, 2),
        "eta_seconds": eta,
    }
