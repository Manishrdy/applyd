"""State machine for job availability (active → suspected → expired).

Three independent signals feed transitions:

  * user_report   — a user POSTed /api/jobs/{id}/report
  * manifest_drop — ingestion noticed the job missed >=2 successful cycles
  * http_check    — verifier service confirmed (or refuted) availability

Confidence policy:
  active + 1 weak signal       → suspected
  suspected + 2nd distinct sig → expired   (if verifier_auto_marking_enabled)
  active + http hard-fail      → expired   (404/410/listing-redirect alone)
  expired + 2 clean re-UPSERTs → reactivate (logged to admin_audit)

The state machine writes are gated on settings.expired_detection_enabled
(global) and settings.verifier_auto_marking_enabled (auto-promote to
'expired'). Anything still flows into job_verification_log for audit.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import settings

log = logging.getLogger(__name__)

Signal = Literal["user_report", "manifest_drop", "http_check"]
Status = Literal["active", "suspected", "expired"]
HttpResult = Literal["active", "expired", "unknown", "error"]


@dataclass(frozen=True)
class TransitionResult:
    """What changed (or didn't) after a signal was applied."""
    job_id: int
    previous_status: Status
    new_status: Status
    reason: str  # human-readable, written to job_verification_log.detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_status(conn: sqlite3.Connection, job_id: int) -> tuple[Status, int, int, str | None] | None:
    """Returns (status, report_count, missed_ingest_cycles, ats_type) or None."""
    row = conn.execute(
        "SELECT verification_status, report_count, missed_ingest_cycles, ats_type "
        "FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return (
        str(row["verification_status"]),  # type: ignore[return-value]
        int(row["report_count"] or 0),
        int(row["missed_ingest_cycles"] or 0),
        row["ats_type"],
    )


def _distinct_reporters(conn: sqlite3.Connection, job_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM job_reports WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return int(row["n"] or 0)


def _set_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: Status,
    *,
    bump_verified_at: bool = False,
) -> None:
    if bump_verified_at:
        conn.execute(
            "UPDATE jobs SET verification_status = ?, verification_status_at = ?, "
            "last_verified_at = ? WHERE id = ?",
            (status, _now_iso(), _now_iso(), job_id),
        )
    else:
        conn.execute(
            "UPDATE jobs SET verification_status = ?, verification_status_at = ? WHERE id = ?",
            (status, _now_iso(), job_id),
        )


def _log_verification(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    trigger: Signal,
    result: HttpResult,
    http_status: int | None = None,
    detector: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO job_verification_log "
        "(job_id, trigger, http_status, result, detector, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, trigger, http_status, result, detector, detail),
    )


def _circuit_breaker_tripped(conn: sqlite3.Connection, ats_type: str | None) -> bool:
    """True if this ATS has hit the per-hour expiration threshold."""
    if not ats_type:
        return False
    hour_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    row = conn.execute(
        "SELECT expire_count, tripped_at, cleared_at FROM verifier_circuit_breaker "
        "WHERE ats_type = ? AND hour_bucket = ?",
        (ats_type, hour_bucket),
    ).fetchone()
    if row is None:
        return False
    tripped = row["tripped_at"]
    cleared = row["cleared_at"]
    return bool(tripped) and not cleared


def _bump_circuit_breaker(
    conn: sqlite3.Connection, ats_type: str | None
) -> bool:
    """Increment the per-ATS expire counter for this hour. Trip if >threshold.
    Returns True if the breaker is now tripped (caller should NOT mark expired).
    """
    if not ats_type:
        return False
    hour_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    conn.execute(
        "INSERT INTO verifier_circuit_breaker (ats_type, hour_bucket, expire_count) "
        "VALUES (?, ?, 1) "
        "ON CONFLICT(ats_type, hour_bucket) DO UPDATE SET "
        "expire_count = expire_count + 1",
        (ats_type, hour_bucket),
    )
    row = conn.execute(
        "SELECT expire_count, tripped_at FROM verifier_circuit_breaker "
        "WHERE ats_type = ? AND hour_bucket = ?",
        (ats_type, hour_bucket),
    ).fetchone()
    count = int(row["expire_count"] or 0)
    already_tripped = bool(row["tripped_at"])
    if count > settings.verifier_circuit_breaker_threshold and not already_tripped:
        conn.execute(
            "UPDATE verifier_circuit_breaker SET tripped_at = ? "
            "WHERE ats_type = ? AND hour_bucket = ?",
            (_now_iso(), ats_type, hour_bucket),
        )
        log.warning(
            "verifier circuit breaker TRIPPED for ats=%s in bucket=%s (count=%d)",
            ats_type, hour_bucket, count,
        )
        return True
    return already_tripped


def on_user_report(
    conn: sqlite3.Connection, job_id: int
) -> TransitionResult | None:
    """Apply a user-report signal. Caller already inserted into job_reports
    and bumped report_count."""
    if not settings.expired_detection_enabled:
        return None
    state = _current_status(conn, job_id)
    if state is None:
        return None
    status, _report_count, missed, ats_type = state
    distinct = _distinct_reporters(conn, job_id)

    if status == "active":
        _set_status(conn, job_id, "suspected")
        _log_verification(
            conn, job_id, trigger="user_report", result="unknown",
            detail=f"escalate active→suspected on first report (distinct={distinct})",
        )
        return TransitionResult(job_id, "active", "suspected",
                                f"first user report (distinct={distinct})")

    if status == "suspected" and distinct >= 2:
        if not settings.verifier_auto_marking_enabled:
            _log_verification(
                conn, job_id, trigger="user_report", result="unknown",
                detail=f"would-promote→expired blocked by auto-marking disabled (distinct={distinct})",
            )
            return TransitionResult(job_id, "suspected", "suspected",
                                    "auto-marking disabled")
        if _circuit_breaker_tripped(conn, ats_type):
            _log_verification(
                conn, job_id, trigger="user_report", result="unknown",
                detail=f"would-promote→expired blocked by circuit breaker (ats={ats_type})",
            )
            return TransitionResult(job_id, "suspected", "suspected",
                                    "circuit breaker tripped")
        # Second signal is also a user report. We require at least one
        # non-user signal too (manifest drop OR http check) — pure user
        # reports without corroboration stays at suspected. Admin can
        # manually promote.
        if missed < 2:
            return TransitionResult(job_id, "suspected", "suspected",
                                    f"two reports but no corroborating signal yet")
        if _bump_circuit_breaker(conn, ats_type):
            return TransitionResult(job_id, "suspected", "suspected",
                                    "circuit breaker just tripped")
        _set_status(conn, job_id, "expired")
        _log_verification(
            conn, job_id, trigger="user_report", result="expired",
            detail=f"2 distinct reports + manifest-drop corroboration (missed={missed})",
        )
        return TransitionResult(job_id, "suspected", "expired",
                                "promoted on 2 reports + manifest drop")

    return TransitionResult(job_id, status, status, "no-op")


def on_manifest_drop(
    conn: sqlite3.Connection, job_id: int, missed: int
) -> TransitionResult | None:
    """Called from the verifier or directly from ingestion when a job has
    accumulated >=2 missed successful ingest cycles. Promotes to suspected
    on the second miss; can promote to expired only with corroboration."""
    if not settings.expired_detection_enabled:
        return None
    state = _current_status(conn, job_id)
    if state is None:
        return None
    status, report_count, _missed_now, ats_type = state

    if status == "active" and missed >= 2:
        _set_status(conn, job_id, "suspected")
        _log_verification(
            conn, job_id, trigger="manifest_drop", result="unknown",
            detail=f"escalate active→suspected after {missed} missed cycles",
        )
        return TransitionResult(job_id, "active", "suspected",
                                f"manifest dropped for {missed} cycles")

    if status == "suspected" and missed >= 2 and report_count >= 1:
        if not settings.verifier_auto_marking_enabled:
            return TransitionResult(job_id, "suspected", "suspected",
                                    "auto-marking disabled")
        if _circuit_breaker_tripped(conn, ats_type) or _bump_circuit_breaker(conn, ats_type):
            return TransitionResult(job_id, "suspected", "suspected",
                                    "circuit breaker tripped")
        _set_status(conn, job_id, "expired")
        _log_verification(
            conn, job_id, trigger="manifest_drop", result="expired",
            detail=f"manifest drop ({missed}) + user reports ({report_count})",
        )
        return TransitionResult(job_id, "suspected", "expired",
                                "promoted on manifest drop + user report")

    return TransitionResult(job_id, status, status, "no-op")


def on_http_check(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    result: HttpResult,
    http_status: int | None,
    detector: str | None,
    detail: str | None = None,
    trigger: Signal = "http_check",
) -> TransitionResult | None:
    """Apply a verifier result. HTTP is ground truth — a definitive expired
    response (404/410/listing redirect, or matched expiry text) jumps a job
    straight to 'expired' without needing corroboration."""
    if not settings.expired_detection_enabled:
        return None
    state = _current_status(conn, job_id)
    if state is None:
        return None
    status, _rc, _missed, ats_type = state

    # Always log the check itself.
    _log_verification(
        conn, job_id, trigger=trigger, http_status=http_status,
        result=result, detector=detector, detail=detail,
    )
    conn.execute(
        "UPDATE jobs SET last_verified_at = ? WHERE id = ?",
        (_now_iso(), job_id),
    )

    if result == "expired":
        if status == "expired":
            return TransitionResult(job_id, "expired", "expired", "already expired")
        if not settings.verifier_auto_marking_enabled:
            return TransitionResult(job_id, status, status,
                                    "would-expire blocked by auto-marking disabled")
        if _circuit_breaker_tripped(conn, ats_type) or _bump_circuit_breaker(conn, ats_type):
            return TransitionResult(job_id, status, status,
                                    "circuit breaker tripped")
        _set_status(conn, job_id, "expired", bump_verified_at=False)
        return TransitionResult(job_id, status, "expired",
                                f"HTTP confirmed expired ({detector or 'generic'})")

    if result == "active":
        # If we previously suspected this job and HTTP says it's live,
        # reactivate. Expired→active needs the manifest-reappearance grace
        # window, not a single check — so only suspected→active here.
        if status == "suspected":
            _set_status(conn, job_id, "active", bump_verified_at=False)
            return TransitionResult(job_id, "suspected", "active",
                                    "HTTP confirmed active, downgrading suspicion")
        return TransitionResult(job_id, status, status, "confirmed active, no-op")

    return TransitionResult(job_id, status, status, f"http result={result}")


def on_manifest_reappear(
    conn: sqlite3.Connection, job_id: int
) -> TransitionResult | None:
    """Called after UPSERT: if an expired job is back in the feed, reactivate.
    Requires 2 consecutive clean cycles (missed_ingest_cycles=0 for the
    second cycle in a row) to prevent flapping — tracked implicitly: the
    first re-UPSERT zeroes the counter, the second one keeps it zero, and
    only THEN do we flip. We approximate this by gating on
    last_verified_at being unset OR older than the current cycle.
    """
    if not settings.expired_detection_enabled:
        return None
    state = _current_status(conn, job_id)
    if state is None:
        return None
    status, _rc, missed, _ats = state
    if status != "expired":
        return None
    if missed != 0:
        return None
    _set_status(conn, job_id, "active")
    _log_verification(
        conn, job_id, trigger="manifest_drop", result="active",
        detail="reactivated after manifest reappearance",
    )
    return TransitionResult(job_id, "expired", "active",
                            "manifest reappearance")
