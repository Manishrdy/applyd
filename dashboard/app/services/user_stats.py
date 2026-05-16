"""Per-user activity stats: by-status counts, saves-per-day, top facets, conversion."""

from __future__ import annotations

from datetime import date, timedelta

from app.database import get_db
from app.schemas import (
    ByStatusCounts,
    MyStatsResponse,
    SavesPerDayPoint,
    TopCount,
)


TRAILING_DAYS = 30


def compute_user_stats(user_id: int) -> MyStatsResponse:
    with get_db() as conn:
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM saved_jobs "
            "WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
        raw = {r["status"]: int(r["n"]) for r in status_rows}
        by_status = ByStatusCounts(
            queued=raw.get("queued", 0),
            applied=raw.get("applied", 0),
            skipped=raw.get("skipped", 0),
            archived=raw.get("archived", 0),
        )
        total = sum(raw.values())
        conversion_rate = (by_status.applied / total) if total > 0 else 0.0

        rows = conn.execute(
            "SELECT date(saved_at) AS d, COUNT(*) AS n FROM saved_jobs "
            "WHERE user_id = ? AND saved_at >= datetime('now', ?) "
            "GROUP BY date(saved_at)",
            (user_id, f"-{TRAILING_DAYS} days"),
        ).fetchall()
        per_day = {r["d"]: int(r["n"]) for r in rows}
        today = date.today()
        saves_per_day = [
            SavesPerDayPoint(
                date=(today - timedelta(days=i)).isoformat(),
                count=per_day.get((today - timedelta(days=i)).isoformat(), 0),
            )
            for i in range(TRAILING_DAYS - 1, -1, -1)
        ]

        company_rows = conn.execute(
            "SELECT j.company AS k, COUNT(*) AS n "
            "FROM saved_jobs s JOIN jobs j ON j.id = s.job_id "
            "WHERE s.user_id = ? AND j.company IS NOT NULL AND j.company != '' "
            "GROUP BY j.company ORDER BY n DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        top_companies = [TopCount(key=r["k"], count=int(r["n"])) for r in company_rows]

        ats_rows = conn.execute(
            "SELECT j.ats_type AS k, COUNT(*) AS n "
            "FROM saved_jobs s JOIN jobs j ON j.id = s.job_id "
            "WHERE s.user_id = ? AND j.ats_type IS NOT NULL "
            "GROUP BY j.ats_type ORDER BY n DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        top_ats = [TopCount(key=r["k"], count=int(r["n"])) for r in ats_rows]

    return MyStatsResponse(
        total_saved=total,
        by_status=by_status,
        saves_per_day=saves_per_day,
        top_companies=top_companies,
        top_ats=top_ats,
        conversion_rate=conversion_rate,
    )
