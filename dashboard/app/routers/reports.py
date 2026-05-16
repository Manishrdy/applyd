"""User-facing job-report API.

POST /api/jobs/{job_id}/report   — submit/upsert a report
DELETE /api/jobs/{job_id}/report — withdraw your own report

Reports feed the expiry-detection lifecycle in app/services/job_lifecycle.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import settings
from app.database import get_db
from app.identity.auth import require_user
from app.services import job_lifecycle

router = APIRouter()

VALID_REASONS = {"not_found", "position_filled", "link_broken", "other"}

# Cap user-supplied free text; protects the moderation UI and prevents
# obvious PII (email, phone) from ending up in admin views.
DETAIL_MAX_LEN = 280
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _strip_pii(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    cleaned = _EMAIL_RE.sub("[email]", cleaned)
    cleaned = _PHONE_RE.sub("[phone]", cleaned)
    return cleaned[:DETAIL_MAX_LEN]


class ReportRequest(BaseModel):
    reason: str = Field(..., description="not_found | position_filled | link_broken | other")
    detail: str | None = Field(None, max_length=DETAIL_MAX_LEN * 2)


class ReportResponse(BaseModel):
    job_id: int
    reported: bool
    verification_status: str
    report_count: int


def _check_rate_limits(conn, user_id: int, company: str | None) -> None:
    """Two caps: total reports per user per 24h, and per-user-per-company per
    7d. Both share the user_action_rate_limits table.
    """
    day_window_iso = (
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    ).isoformat()
    day_key = f"report:day:{user_id}"
    row = conn.execute(
        "SELECT count, window_started_at FROM user_action_rate_limits WHERE bucket_key = ?",
        (day_key,),
    ).fetchone()
    if row and row["window_started_at"] >= day_window_iso:
        if int(row["count"]) >= settings.report_rate_limit_per_day:
            raise HTTPException(429, "report rate limit reached for today")
        conn.execute(
            "UPDATE user_action_rate_limits SET count = count + 1 WHERE bucket_key = ?",
            (day_key,),
        )
    else:
        conn.execute(
            "INSERT INTO user_action_rate_limits (bucket_key, count, window_started_at) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(bucket_key) DO UPDATE SET "
            "count = 1, window_started_at = excluded.window_started_at",
            (day_key, datetime.now(timezone.utc).isoformat()),
        )

    if company:
        week_window_iso = (datetime.now(timezone.utc).isoformat()[:10])
        co_key = f"report:co:{user_id}:{company.lower()}"
        row = conn.execute(
            "SELECT count, window_started_at FROM user_action_rate_limits WHERE bucket_key = ?",
            (co_key,),
        ).fetchone()
        # Roll the window every 7 days from the first hit in the bucket.
        if row:
            try:
                start = datetime.fromisoformat(str(row["window_started_at"]))
            except ValueError:
                start = datetime.now(timezone.utc)
            age_days = (datetime.now(timezone.utc) - start.replace(tzinfo=timezone.utc) if start.tzinfo is None else datetime.now(timezone.utc) - start).days
            if age_days < 7:
                if int(row["count"]) >= settings.report_rate_limit_per_company_per_week:
                    raise HTTPException(
                        429, "report rate limit reached for this company"
                    )
                conn.execute(
                    "UPDATE user_action_rate_limits SET count = count + 1 WHERE bucket_key = ?",
                    (co_key,),
                )
                return
        conn.execute(
            "INSERT INTO user_action_rate_limits (bucket_key, count, window_started_at) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(bucket_key) DO UPDATE SET "
            "count = 1, window_started_at = excluded.window_started_at",
            (co_key, datetime.now(timezone.utc).isoformat()),
        )


@router.post("/{job_id}/report", response_model=ReportResponse)
def report_job(
    job_id: int,
    body: ReportRequest,
    user_id: int = Depends(require_user),
) -> ReportResponse:
    if body.reason not in VALID_REASONS:
        raise HTTPException(400, f"invalid reason; expected one of {sorted(VALID_REASONS)}")
    detail = _strip_pii(body.detail)

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, company, verification_status, report_count FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        company = row["company"]

        _check_rate_limits(conn, user_id, company)

        cur = conn.execute(
            "INSERT INTO job_reports (user_id, job_id, reason, detail) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, job_id) DO UPDATE SET "
            "reason = excluded.reason, detail = excluded.detail",
            (user_id, job_id, body.reason, detail),
        )
        was_insert = cur.rowcount == 1 and conn.execute(
            "SELECT changes()"
        ).fetchone()[0] == 1
        # SQLite reports rowcount=1 for both INSERT and DO UPDATE; distinguish
        # by checking whether the row already had a different reporter count.
        # Cheaper: re-query distinct reporters before/after — but the count
        # increment is idempotent against a UNIQUE collision so simpler to
        # recompute total report_count from job_reports.
        new_count = int(conn.execute(
            "SELECT COUNT(*) FROM job_reports WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0])
        conn.execute(
            "UPDATE jobs SET report_count = ? WHERE id = ?",
            (new_count, job_id),
        )

        job_lifecycle.on_user_report(conn, job_id)
        new_status_row = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    return ReportResponse(
        job_id=job_id,
        reported=True,
        verification_status=str(new_status_row["verification_status"]),
        report_count=new_count,
    )


@router.delete("/{job_id}/report", response_model=ReportResponse)
def withdraw_report(
    job_id: int,
    user_id: int = Depends(require_user),
) -> ReportResponse:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM job_reports WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "report not found")
        new_count = int(conn.execute(
            "SELECT COUNT(*) FROM job_reports WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0])
        conn.execute(
            "UPDATE jobs SET report_count = ? WHERE id = ?",
            (new_count, job_id),
        )
        status_row = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        verification_status = str(status_row["verification_status"]) if status_row else "active"

    return ReportResponse(
        job_id=job_id,
        reported=False,
        verification_status=verification_status,
        report_count=new_count,
    )
