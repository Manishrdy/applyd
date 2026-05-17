"""Admin moderation surface for user-submitted job-availability reports.

Endpoints:
  GET  /api/admin/job-reports                 — paginated review queue
  POST /api/admin/jobs/{job_id}/expire        — manually mark expired
  POST /api/admin/jobs/{job_id}/reactivate    — flip expired back to active
  GET  /api/admin/job-reports/reporters       — users with anomalous report rate
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.config import settings
from app.database import get_db
from app.services import verifier as verifier_svc

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/job-reports")
def list_job_reports(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
    status: Annotated[str | None, Query(description="active | suspected | expired")] = None,
    min_reports: Annotated[int, Query(ge=0)] = 1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Reports grouped by job, ordered by most-recently-reported."""
    conditions = ["j.report_count >= ?"]
    params: list = [min_reports]
    if status:
        conditions.append("j.verification_status = ?")
        params.append(status)
    where = " AND ".join(conditions)
    sql = (
        "SELECT j.id, j.url, j.title, j.company, j.country, j.ats_type, "
        "j.verification_status, j.verification_status_at, j.report_count, "
        "j.missed_ingest_cycles, j.last_seen_in_manifest_at, j.last_verified_at, "
        "(SELECT COUNT(DISTINCT user_id) FROM job_reports r WHERE r.job_id = j.id) AS distinct_reporters, "
        "(SELECT MAX(reported_at) FROM job_reports r WHERE r.job_id = j.id) AS last_reported_at "
        f"FROM jobs j WHERE {where} "
        "ORDER BY last_reported_at DESC NULLS LAST "
        "LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    count_sql = f"SELECT COUNT(*) FROM jobs j WHERE {where}"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = int(conn.execute(count_sql, params[:-2]).fetchone()[0])
    return {
        "reports": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/job-reports/{job_id}")
def job_report_detail(
    job_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    """All individual reports for one job, plus its full verification log."""
    with get_db() as conn:
        job = conn.execute(
            "SELECT id, url, title, company, country, ats_type, "
            "verification_status, verification_status_at, report_count, "
            "missed_ingest_cycles, last_seen_in_manifest_at, last_verified_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not job:
            raise HTTPException(404, "job not found")
        reports = conn.execute(
            "SELECT r.id, r.user_id, u.email AS user_email, r.reason, r.detail, r.reported_at "
            "FROM job_reports r LEFT JOIN users u ON u.id = r.user_id "
            "WHERE r.job_id = ? ORDER BY r.reported_at DESC",
            (job_id,),
        ).fetchall()
        log_rows = conn.execute(
            "SELECT checked_at, trigger, http_status, result, detector, detail "
            "FROM job_verification_log WHERE job_id = ? "
            "ORDER BY checked_at DESC LIMIT 50",
            (job_id,),
        ).fetchall()
    return {
        "job": dict(job),
        "reports": [dict(r) for r in reports],
        "verification_log": [dict(r) for r in log_rows],
    }


@router.post("/jobs/{job_id}/expire")
def admin_expire_job(
    job_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    admin: AdminUser = Depends(require_admin_user),
):
    """Manually mark a job expired. Writes admin_audit."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        previous = str(row["verification_status"])
        conn.execute(
            "UPDATE jobs SET verification_status = 'expired', "
            "verification_status_at = ? WHERE id = ?",
            (now_iso, job_id),
        )
        conn.execute(
            "INSERT INTO job_verification_log (job_id, trigger, result, detector, detail) "
            "VALUES (?, 'admin', 'expired', 'admin_override', ?)",
            (job_id, f"admin {admin.email} forced expire from {previous}"),
        )
    audit.record(
        admin=admin,
        action="expire_job",
        target=str(job_id),
        detail={"previous_status": previous},
        request=request,
    )
    return {"job_id": job_id, "verification_status": "expired"}


@router.post("/jobs/{job_id}/reactivate")
def admin_reactivate_job(
    job_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    admin: AdminUser = Depends(require_admin_user),
):
    """Force a job back to active. Resets missed_ingest_cycles too."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        previous = str(row["verification_status"])
        conn.execute(
            "UPDATE jobs SET verification_status = 'active', "
            "verification_status_at = ?, missed_ingest_cycles = 0 "
            "WHERE id = ?",
            (now_iso, job_id),
        )
        conn.execute(
            "INSERT INTO job_verification_log (job_id, trigger, result, detector, detail) "
            "VALUES (?, 'admin', 'active', 'admin_override', ?)",
            (job_id, f"admin {admin.email} reactivated from {previous}"),
        )
    audit.record(
        admin=admin,
        action="reactivate_job",
        target=str(job_id),
        detail={"previous_status": previous},
        request=request,
    )
    return {"job_id": job_id, "verification_status": "active"}


@router.get("/job-reports-reporters")
def anomalous_reporters(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
    min_reports: Annotated[int, Query(ge=1)] = 10,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Users whose report rate stands out — surface for abuse review."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT r.user_id, u.email AS user_email, "
            "COUNT(*) AS report_count, "
            "MIN(r.reported_at) AS first_reported_at, "
            "MAX(r.reported_at) AS last_reported_at "
            "FROM job_reports r LEFT JOIN users u ON u.id = r.user_id "
            "GROUP BY r.user_id "
            "HAVING report_count >= ? "
            "ORDER BY report_count DESC LIMIT ?",
            (min_reports, limit),
        ).fetchall()
    return {"reporters": [dict(r) for r in rows]}


def _build_expirations_snapshot() -> dict:
    """Pure builder for the expirations dashboard payload.

    Factored out of the HTTP handler so the SSE stream can call it on a
    timer without going through FastAPI again. Self-contained: opens its
    own DB connection. Returns counts, three activity windows, per-ATS
    and per-detector matrices, the last tick, schedule config, breakers,
    and the most recent verification log rows.
    """
    now = datetime.now(timezone.utc)
    hour_bucket = now.strftime("%Y-%m-%dT%H")
    midnight_iso = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    hour_ago_iso = (now - timedelta(hours=1)).isoformat()
    day_ago_iso = (now - timedelta(hours=24)).isoformat()

    def _result_breakdown(rows) -> dict[str, int]:
        out = {"active": 0, "expired": 0, "unknown": 0, "error": 0}
        for r in rows:
            key = str(r["result"])
            out[key] = int(r["n"])
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out

    with get_db() as conn:
        # Status counts on jobs table.
        counts_rows = conn.execute(
            "SELECT verification_status AS status, COUNT(*) AS n "
            "FROM jobs GROUP BY verification_status"
        ).fetchall()
        counts = {str(r["status"]): int(r["n"]) for r in counts_rows}
        for k in ("active", "suspected", "expired"):
            counts.setdefault(k, 0)

        # Verifier activity windows.
        today_rows = conn.execute(
            "SELECT result, COUNT(*) AS n FROM job_verification_log "
            "WHERE julianday(checked_at) >= julianday(?) GROUP BY result",
            (midnight_iso,),
        ).fetchall()
        hour_rows = conn.execute(
            "SELECT result, COUNT(*) AS n FROM job_verification_log "
            "WHERE julianday(checked_at) >= julianday(?) GROUP BY result",
            (hour_ago_iso,),
        ).fetchall()
        day_rows = conn.execute(
            "SELECT result, COUNT(*) AS n FROM job_verification_log "
            "WHERE julianday(checked_at) >= julianday(?) GROUP BY result",
            (day_ago_iso,),
        ).fetchall()

        # Per-ATS breakdown today.
        per_ats_rows = conn.execute(
            "SELECT j.ats_type, l.result, COUNT(*) AS n "
            "FROM job_verification_log l "
            "JOIN jobs j ON j.id = l.job_id "
            "WHERE julianday(l.checked_at) >= julianday(?) "
            "GROUP BY j.ats_type, l.result "
            "ORDER BY n DESC LIMIT 200",
            (midnight_iso,),
        ).fetchall()
        per_ats: dict[str, dict[str, int]] = {}
        for r in per_ats_rows:
            bucket = per_ats.setdefault(
                str(r["ats_type"] or "(unknown)"),
                {"active": 0, "expired": 0, "unknown": 0, "error": 0, "total": 0},
            )
            key = str(r["result"])
            bucket[key] = int(r["n"])
            bucket["total"] += int(r["n"])

        # Per-detector breakdown today.
        per_detector_rows = conn.execute(
            "SELECT detector, result, COUNT(*) AS n FROM job_verification_log "
            "WHERE julianday(checked_at) >= julianday(?) "
            "GROUP BY detector, result "
            "ORDER BY n DESC LIMIT 100",
            (midnight_iso,),
        ).fetchall()
        per_detector: dict[str, dict[str, int]] = {}
        for r in per_detector_rows:
            bucket = per_detector.setdefault(
                str(r["detector"] or "(none)"),
                {"active": 0, "expired": 0, "unknown": 0, "error": 0, "total": 0},
            )
            key = str(r["result"])
            bucket[key] = int(r["n"])
            bucket["total"] += int(r["n"])

        last_tick_row = conn.execute(
            "SELECT checked_at, result, trigger, detector FROM job_verification_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        recent_rows = conn.execute(
            "SELECT l.id, l.job_id, l.checked_at, l.trigger, l.http_status, "
            "l.result, l.detector, l.detail, j.title, j.company, j.ats_type "
            "FROM job_verification_log l "
            "LEFT JOIN jobs j ON j.id = l.job_id "
            "ORDER BY l.id DESC LIMIT 30"
        ).fetchall()

        breaker_rows = conn.execute(
            "SELECT ats_type, hour_bucket, expire_count, tripped_at, cleared_at "
            "FROM verifier_circuit_breaker "
            "WHERE hour_bucket = ? OR tripped_at IS NOT NULL "
            "ORDER BY hour_bucket DESC, expire_count DESC LIMIT 50",
            (hour_bucket,),
        ).fetchall()

    active = counts.get("active", 0)
    sweep_days = max(1, settings.verifier_sweep_days)
    interval_min = max(1, settings.verifier_sweep_interval_minutes)
    ticks_per_window = (sweep_days * 24 * 60) // interval_min
    per_tick = max(1, active // max(1, ticks_per_window)) if active else 0
    schedule = {
        "expired_detection_enabled": settings.expired_detection_enabled,
        "verifier_auto_marking_enabled": settings.verifier_auto_marking_enabled,
        "sweep_all_active": settings.verifier_sweep_all_active,
        "sweep_days": settings.verifier_sweep_days,
        "sweep_interval_minutes": settings.verifier_sweep_interval_minutes,
        "suspected_interval_minutes": settings.verifier_suspected_interval_minutes,
        "active_corpus_size": active,
        "est_jobs_per_tick": per_tick,
        "est_ticks_per_full_pass": ticks_per_window,
    }
    sweep_state = verifier_svc.get_run_state()

    return {
        "counts": counts,
        "today": _result_breakdown(today_rows),
        "last_hour": _result_breakdown(hour_rows),
        "last_24h": _result_breakdown(day_rows),
        "per_ats": per_ats,
        "per_detector": per_detector,
        "last_tick": dict(last_tick_row) if last_tick_row else None,
        "schedule": schedule,
        "sweep_state": sweep_state,
        "breakers": [dict(r) for r in breaker_rows],
        "recent": [dict(r) for r in recent_rows],
    }


@router.get("/expirations/summary")
def expirations_summary(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    """One-shot snapshot endpoint. The SSE stream below re-uses the same builder."""
    return _build_expirations_snapshot()


# ─── SSE stream (replaces 30s polling) ──────────────────────────────────────

EXPIRATIONS_STREAM_INTERVAL_SECONDS = 5.0      # snapshot cadence
EXPIRATIONS_STREAM_HEARTBEAT_SECONDS = 15.0    # idle keepalive
EXPIRATIONS_STREAM_MAX_SECONDS = 300           # 5-min stream lifetime


def _sse_frame(event: str | None, data) -> str:
    body = data if isinstance(data, str) else json.dumps(data, default=str, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n"


async def _expirations_event_stream(request: Request):
    """Push expirations snapshots every ~5s. Mirror of the /stream/health
    pattern in app/admin/routers/system.py — same heartbeat, same bounded
    lifetime so an idle browser eventually rotates the cookie."""
    start = time.monotonic()
    next_data_at = start
    sent_events = 0
    log.info("expirations SSE stream opened")
    try:
        yield _sse_frame("hello", {"interval_seconds": EXPIRATIONS_STREAM_INTERVAL_SECONDS})
        while True:
            if await request.is_disconnected():
                return
            now = time.monotonic()
            if now - start >= EXPIRATIONS_STREAM_MAX_SECONDS:
                yield _sse_frame("timeout", {"reason": "max_lifetime_reached"})
                return
            if now >= next_data_at:
                try:
                    snapshot = _build_expirations_snapshot()
                except Exception:
                    log.exception("expirations snapshot failed; skipping tick")
                else:
                    yield _sse_frame(None, snapshot)
                    sent_events += 1
                next_data_at = now + EXPIRATIONS_STREAM_INTERVAL_SECONDS
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(min(
                EXPIRATIONS_STREAM_HEARTBEAT_SECONDS,
                max(0.05, next_data_at - time.monotonic()),
            ))
    except asyncio.CancelledError:
        log.info("expirations SSE stream cancelled (events_sent=%d)", sent_events)
        raise
    finally:
        log.info("expirations SSE stream closed (events_sent=%d)", sent_events)


@router.get("/stream/expirations")
async def admin_stream_expirations(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    return StreamingResponse(
        _expirations_event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Expired-job review + bulk operations ──────────────────────────────────


def _review_where_clauses(
    ats: list[str] | None,
    country: list[str] | None,
    detector: list[str] | None,
    reason: list[str] | None,
    expired_before: str | None,
    expired_after: str | None,
    company: str | None,
) -> tuple[list[str], list]:
    """Build the WHERE for the expired-review filter.

    Only jobs in 'expired' state are ever returned — that's the whole point
    of the review surface.
    """
    conditions = ["j.verification_status = 'expired'"]
    params: list = []
    if ats:
        ph = ",".join(["?"] * len(ats))
        conditions.append(f"j.ats_type IN ({ph})")
        params.extend(ats)
    if country:
        ph = ",".join(["?"] * len(country))
        conditions.append(f"j.country IN ({ph})")
        params.extend(country)
    if company:
        conditions.append("j.company = ?")
        params.append(company)
    if expired_after:
        conditions.append("j.verification_status_at >= ?")
        params.append(expired_after)
    if expired_before:
        conditions.append("j.verification_status_at <= ?")
        params.append(expired_before)
    if detector:
        # Match against the latest verification_log entry for each job.
        # Subquery instead of a join so the OR/IN pushes down cleanly.
        ph = ",".join(["?"] * len(detector))
        conditions.append(
            "EXISTS (SELECT 1 FROM job_verification_log l "
            "WHERE l.job_id = j.id AND l.result = 'expired' "
            f"AND l.detector IN ({ph}))"
        )
        params.extend(detector)
    if reason:
        # `reason` here = which trigger flipped the job to expired.
        # user_report | manifest_drop | http_check | admin
        ph = ",".join(["?"] * len(reason))
        conditions.append(
            "EXISTS (SELECT 1 FROM job_verification_log l "
            "WHERE l.job_id = j.id AND l.result = 'expired' "
            f"AND l.trigger IN ({ph}))"
        )
        params.extend(reason)
    return conditions, params


@router.get("/expirations/review")
def expirations_review(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
    ats: Annotated[list[str] | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    detector: Annotated[list[str] | None, Query()] = None,
    reason: Annotated[list[str] | None, Query(description="user_report | manifest_drop | http_check | admin")] = None,
    company: Annotated[str | None, Query()] = None,
    expired_after: Annotated[str | None, Query(description="ISO8601 UTC; jobs.verification_status_at >= this")] = None,
    expired_before: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query(description="newest | oldest")] = "newest",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Filterable list of verified-expired jobs + same-filter group stats.

    The response contains BOTH:
      * `jobs`         — the paginated rows
      * `group_stats`  — counts grouped by ATS / country / detector / reason
                         under the *same* filter set, so the sidebar matches
                         the table.
      * `total`        — total matching count (drives the type-to-confirm
                         number for bulk delete)
    """
    conditions, params = _review_where_clauses(
        ats, country, detector, reason, expired_before, expired_after, company,
    )
    where = " AND ".join(conditions)
    order = "j.verification_status_at DESC" if sort != "oldest" else "j.verification_status_at ASC"

    rows_sql = (
        "SELECT j.id, j.url, j.title, j.company, j.country, j.ats_type, "
        "j.verification_status_at, j.report_count, "
        "(SELECT l.trigger || ':' || COALESCE(l.detector, '') FROM job_verification_log l "
        "  WHERE l.job_id = j.id AND l.result = 'expired' "
        "  ORDER BY l.id DESC LIMIT 1) AS expired_reason, "
        "(SELECT l.detail FROM job_verification_log l "
        "  WHERE l.job_id = j.id AND l.result = 'expired' "
        "  ORDER BY l.id DESC LIMIT 1) AS expired_detail "
        f"FROM jobs j WHERE {where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?"
    )
    count_sql = f"SELECT COUNT(*) FROM jobs j WHERE {where}"
    by_ats_sql = (
        f"SELECT j.ats_type, COUNT(*) AS n FROM jobs j WHERE {where} "
        "GROUP BY j.ats_type ORDER BY n DESC"
    )
    by_country_sql = (
        f"SELECT j.country, COUNT(*) AS n FROM jobs j WHERE {where} "
        "GROUP BY j.country ORDER BY n DESC"
    )
    by_company_sql = (
        f"SELECT j.company, COUNT(*) AS n FROM jobs j WHERE {where} "
        "GROUP BY j.company ORDER BY n DESC LIMIT 20"
    )
    by_reason_sql = (
        "SELECT l.trigger, COUNT(DISTINCT j.id) AS n "
        f"FROM jobs j JOIN job_verification_log l ON l.job_id = j.id "
        f"WHERE {where} AND l.result = 'expired' "
        "GROUP BY l.trigger ORDER BY n DESC"
    )

    with get_db() as conn:
        rows = conn.execute(rows_sql, params + [limit, offset]).fetchall()
        total = int(conn.execute(count_sql, params).fetchone()[0])
        by_ats = [dict(r) for r in conn.execute(by_ats_sql, params).fetchall()]
        by_country = [dict(r) for r in conn.execute(by_country_sql, params).fetchall()]
        by_company = [dict(r) for r in conn.execute(by_company_sql, params).fetchall()]
        by_reason = [dict(r) for r in conn.execute(by_reason_sql, params).fetchall()]

    return {
        "jobs": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "group_stats": {
            "by_ats": by_ats,
            "by_country": by_country,
            "by_company": by_company,
            "by_reason": by_reason,
        },
    }


def _parse_bulk_filters(filters_json: str) -> dict:
    try:
        return json.loads(filters_json or "{}") or {}
    except json.JSONDecodeError:
        raise HTTPException(400, "filters_json: invalid JSON")


@router.post("/expirations/bulk-delete")
def expirations_bulk_delete(
    request: Request,
    filters_json: Annotated[str, Form()] = "{}",
    confirm_count: Annotated[int, Form()] = 0,
    csrf_token: Annotated[str, Form()] = "",
    admin: AdminUser = Depends(require_admin_user),
):
    """Destructive bulk delete of expired jobs matching the given filters.

    Form fields (so the existing admin.js post helper handles CSRF + cookies
    consistently with the rest of the admin panel):
      filters_json   — JSON string: {ats?, country?, detector?, reason?,
                       company?, expired_after?, expired_before?}
      confirm_count  — must equal the live preview count (type-the-count gate)

    Refuses the operation when confirm_count != the actual preview count.
    Numbers can't be guessed — same protection pattern as the backup-restore
    filename gate.
    """
    filters = _parse_bulk_filters(filters_json)
    confirm_count = int(confirm_count or 0)

    conditions, params = _review_where_clauses(
        filters.get("ats"), filters.get("country"), filters.get("detector"),
        filters.get("reason"), filters.get("expired_before"),
        filters.get("expired_after"), filters.get("company"),
    )
    where = " AND ".join(conditions)

    with get_db() as conn:
        live_count = int(conn.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE {where}", params
        ).fetchone()[0])
        if confirm_count != live_count:
            raise HTTPException(
                400,
                f"confirm_count mismatch: expected {live_count}, got {confirm_count}. "
                f"Refresh the preview and try again.",
            )
        if live_count == 0:
            return {"deleted": 0, "skipped": "no rows matched"}

        # Capture id list so audit detail can log the affected scope.
        # saved_jobs cascades; job_reports.job_id is SET NULL.
        rows = conn.execute(
            f"SELECT j.id FROM jobs j WHERE {where}", params
        ).fetchall()
        ids = [int(r["id"]) for r in rows]

        # Chunk the DELETE since SQLite has a parameter cap. 500/chunk is safe.
        deleted = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i+500]
            ph = ",".join(["?"] * len(chunk))
            cur = conn.execute(f"DELETE FROM jobs WHERE id IN ({ph})", chunk)
            deleted += int(cur.rowcount or 0)

    audit.record(
        admin=admin,
        action="bulk_delete_expired",
        target=f"{deleted} jobs",
        detail={"filters": filters, "deleted_count": deleted},
        request=request,
    )
    return {"deleted": deleted, "ids": ids[:50]}  # truncate for response size


@router.post("/expirations/bulk-reactivate")
def expirations_bulk_reactivate(
    request: Request,
    filters_json: Annotated[str, Form()] = "{}",
    confirm_count: Annotated[int, Form()] = 0,
    csrf_token: Annotated[str, Form()] = "",
    admin: AdminUser = Depends(require_admin_user),
):
    """Flip a filtered set of expired jobs back to 'active'.

    Same shape as bulk-delete: type-the-count confirmation gate, audit log.
    No data loss; flips verification_status + clears missed_ingest_cycles.
    """
    filters = _parse_bulk_filters(filters_json)
    confirm_count = int(confirm_count or 0)

    conditions, params = _review_where_clauses(
        filters.get("ats"), filters.get("country"), filters.get("detector"),
        filters.get("reason"), filters.get("expired_before"),
        filters.get("expired_after"), filters.get("company"),
    )
    where = " AND ".join(conditions)
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        live_count = int(conn.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE {where}", params
        ).fetchone()[0])
        if confirm_count != live_count:
            raise HTTPException(
                400,
                f"confirm_count mismatch: expected {live_count}, got {confirm_count}. "
                f"Refresh the preview and try again.",
            )
        if live_count == 0:
            return {"reactivated": 0, "skipped": "no rows matched"}
        cur = conn.execute(
            f"UPDATE jobs SET verification_status = 'active', "
            f"verification_status_at = ?, missed_ingest_cycles = 0 "
            f"WHERE id IN (SELECT id FROM jobs j WHERE {where})",
            [now_iso, *params],
        )
        reactivated = int(cur.rowcount or 0)

    audit.record(
        admin=admin,
        action="bulk_reactivate_expired",
        target=f"{reactivated} jobs",
        detail={"filters": filters, "reactivated_count": reactivated},
        request=request,
    )
    return {"reactivated": reactivated}


@router.post("/expirations/run-sweep")
async def admin_run_sweep_now(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    batch_size: Annotated[int, Form()] = 0,
    admin: AdminUser = Depends(require_admin_user),
):
    """Manually kick a verifier sweep right now. Useful when --reload has
    been resetting the scheduler and you want immediate proof of life."""
    state = verifier_svc.get_run_state()
    if state.get("running"):
        raise HTTPException(
            409,
            "A verifier job is already running. Stop it first, then run a new sweep.",
        )
    bs = batch_size if batch_size and batch_size > 0 else None
    result = await verifier_svc.drain_periodic_sweep(batch_size=bs)
    audit.record(
        admin=admin,
        action="run_verifier_sweep",
        target="manual",
        detail=result,
        request=request,
    )
    return {"ok": True, "result": result}


@router.get("/expirations/sweep-state")
def admin_sweep_state(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    return {"ok": True, "state": verifier_svc.get_run_state()}


@router.post("/expirations/stop-sweep")
def admin_stop_sweep(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    admin: AdminUser = Depends(require_admin_user),
):
    before = verifier_svc.get_run_state()
    cancelled = verifier_svc.request_cancel_active_run()
    after = verifier_svc.get_run_state()
    audit.record(
        admin=admin,
        action="stop_verifier_sweep",
        target="manual",
        detail={"cancel_requested": cancelled, "before": before, "after": after},
        request=request,
    )
    return {"ok": True, "cancel_requested": cancelled, "state": after}


@router.post("/verifier/circuit-breaker/{ats_type}/clear")
def admin_clear_circuit_breaker(
    ats_type: str,
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    admin: AdminUser = Depends(require_admin_user),
):
    """Clear all tripped circuit breakers for a given ATS."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE verifier_circuit_breaker SET cleared_at = ?, cleared_by = ? "
            "WHERE ats_type = ? AND cleared_at IS NULL AND tripped_at IS NOT NULL",
            (now_iso, admin.id, ats_type),
        )
        cleared = int(cur.rowcount or 0)
    audit.record(
        admin=admin,
        action="clear_circuit_breaker",
        target=ats_type,
        detail={"cleared": cleared},
        request=request,
    )
    return {"ats_type": ats_type, "cleared": cleared}
