"""Analytics endpoints powering the /stats page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.config import settings
from app.database import get_db
from app.schemas import (
    CountByLabel,
    GroupedCounts,
    RemoteVsOnsite,
    SalaryBucket,
    SalaryRangeResponse,
    StatsSummary,
)

router = APIRouter()


_EFFECTIVE_DATE = "COALESCE(posted_at, first_seen_at)"


@router.get("/summary", response_model=StatsSummary)
def summary() -> StatsSummary:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        dated = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE posted_at IS NOT NULL"
        ).fetchone()[0]
        us_total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE country = 'US'"
        ).fetchone()[0]
        us_24h = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE country='US' AND {_EFFECTIVE_DATE} >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        us_7d = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE country='US' AND {_EFFECTIVE_DATE} >= datetime('now', '-7 days')"
        ).fetchone()[0]
        us_30d = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE country='US' AND {_EFFECTIVE_DATE} >= datetime('now', '-30 days')"
        ).fetchone()[0]
        remote = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE is_remote = 1"
        ).fetchone()[0]
        ats_count = conn.execute(
            "SELECT COUNT(DISTINCT ats_type) FROM jobs WHERE ats_type IS NOT NULL"
        ).fetchone()[0]
        company_count = conn.execute(
            "SELECT COUNT(DISTINCT company) FROM jobs WHERE company IS NOT NULL"
        ).fetchone()[0]
        last_sync_row = conn.execute(
            "SELECT fetched_at FROM manifest_log WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return StatsSummary(
        total_jobs=total,
        dated=dated,
        undated=total - dated,
        us_total=us_total,
        us_24h=us_24h,
        us_7d=us_7d,
        us_30d=us_30d,
        remote=remote,
        ats_count=ats_count,
        company_count=company_count,
        last_sync=last_sync_row[0] if last_sync_row else None,
        rolling_window_days=settings.rolling_window_days,
    )


@router.get("/by_ats", response_model=GroupedCounts)
def by_ats(
    days: Annotated[int, Query(ge=0, le=30)] = 30,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> GroupedCounts:
    sql = (
        f"SELECT ats_type AS label, COUNT(*) AS n FROM jobs "
        f"WHERE ats_type IS NOT NULL"
    )
    params: list = []
    if days > 0:
        sql += f" AND {_EFFECTIVE_DATE} >= datetime('now', ?)"
        params.append(f"-{days} days")
    sql += " GROUP BY ats_type ORDER BY n DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return GroupedCounts(items=[CountByLabel(label=r["label"], count=r["n"]) for r in rows])


@router.get("/by_day", response_model=GroupedCounts)
def by_day(
    country: str | None = "US",
    days: Annotated[int, Query(ge=1, le=30)] = 30,
) -> GroupedCounts:
    """Jobs per day, by COALESCE(posted_at, first_seen_at). Drives the
    /stats line chart. Today's bucket is bootstrap-flooded — see
    [[project-data-decisions]] Decision 1.
    """
    # Cap at today: some upstream sources have future-dated `posted_at`
    # (Workday especially) which would stretch the chart's x-axis pointlessly.
    conditions = [
        f"{_EFFECTIVE_DATE} >= datetime('now', ?)",
        f"{_EFFECTIVE_DATE} <= datetime('now')",
    ]
    params: list = [f"-{days} days"]
    if country:
        conditions.append("country = ?")
        params.append(country)
    where = " AND ".join(conditions)
    sql = (
        f"SELECT date({_EFFECTIVE_DATE}) AS day, COUNT(*) AS n "
        f"FROM jobs WHERE {where} "
        f"GROUP BY day ORDER BY day"
    )
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return GroupedCounts(
        items=[CountByLabel(label=r["day"], count=r["n"]) for r in rows]
    )


@router.get("/by_country", response_model=GroupedCounts)
def by_country(
    days: Annotated[int, Query(ge=0, le=30)] = 30,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> GroupedCounts:
    # GROUP BY country (not COALESCE) so SQLite can use idx_jobs_country_eff
    # for an index-only scan. Map NULL → "UNKNOWN" in Python.
    sql = "SELECT country, COUNT(*) AS n FROM jobs WHERE 1=1"
    params: list = []
    if days > 0:
        sql += f" AND {_EFFECTIVE_DATE} >= datetime('now', ?)"
        params.append(f"-{days} days")
    sql += " GROUP BY country ORDER BY n DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return GroupedCounts(
        items=[CountByLabel(label=r["country"] or "UNKNOWN", count=r["n"]) for r in rows]
    )


@router.get("/top_companies", response_model=GroupedCounts)
def top_companies(
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    country: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> GroupedCounts:
    conditions = ["company IS NOT NULL", "company != ''"]
    params: list = []
    if country:
        conditions.append("country = ?")
        params.append(country)
    conditions.append(f"{_EFFECTIVE_DATE} >= datetime('now', ?)")
    params.append(f"-{days} days")
    where = " AND ".join(conditions)
    sql = (
        f"SELECT company AS label, COUNT(*) AS n FROM jobs WHERE {where} "
        f"GROUP BY company ORDER BY n DESC LIMIT ?"
    )
    params.append(limit)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return GroupedCounts(items=[CountByLabel(label=r["label"], count=r["n"]) for r in rows])


@router.get("/salary_range", response_model=SalaryRangeResponse)
def salary_range(
    country: str | None = "US",
    days: Annotated[int, Query(ge=0, le=30)] = 30,
) -> SalaryRangeResponse:
    # Bucket boundaries in annual USD
    buckets_def = [
        (0, 50_000),
        (50_000, 75_000),
        (75_000, 100_000),
        (100_000, 150_000),
        (150_000, 200_000),
        (200_000, 300_000),
        (300_000, 500_000),
        (500_000, None),
    ]
    conditions = ["salary_max_usd_annual IS NOT NULL"]
    params: list = []
    if country:
        conditions.append("country = ?")
        params.append(country)
    if days > 0:
        conditions.append(f"{_EFFECTIVE_DATE} >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)

    buckets: list[SalaryBucket] = []
    with get_db() as conn:
        for low, high in buckets_def:
            bp = params + [low]
            extra = " AND salary_max_usd_annual >= ?"
            if high is not None:
                extra += " AND salary_max_usd_annual < ?"
                bp = params + [low, high]
            n = conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {where}{extra}", bp
            ).fetchone()[0]
            buckets.append(SalaryBucket(low=low, high=high, count=n))

        # Sample-based percentiles. Sorting all matching rows on day-0 is ~3s;
        # a 10K-row recent sample gives within ~5% of true percentile in <100ms.
        sample = conn.execute(
            f"SELECT salary_max_usd_annual FROM jobs WHERE {where} "
            f"ORDER BY id DESC LIMIT 10000", params,
        ).fetchall()
        median = p25 = p75 = None
        if sample:
            sorted_vals = sorted(r[0] for r in sample if r[0] is not None)
            if sorted_vals:
                n = len(sorted_vals)
                p25 = sorted_vals[n // 4]
                median = sorted_vals[n // 2]
                p75 = sorted_vals[3 * n // 4]

    return SalaryRangeResponse(buckets=buckets, median=median, p25=p25, p75=p75)


@router.get("/remote_vs_onsite", response_model=RemoteVsOnsite)
def remote_vs_onsite(
    country: str | None = "US",
    days: Annotated[int, Query(ge=0, le=30)] = 30,
) -> RemoteVsOnsite:
    conditions: list[str] = []
    params: list = []
    if country:
        conditions.append("country = ?")
        params.append(country)
    if days > 0:
        conditions.append(f"{_EFFECTIVE_DATE} >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT is_remote, COUNT(*) AS n FROM jobs{where} GROUP BY is_remote",
            params,
        ).fetchall()
    by = {r["is_remote"]: r["n"] for r in rows}
    return RemoteVsOnsite(
        remote=by.get(1, 0),
        onsite=by.get(0, 0),
        unknown=by.get(None, 0),
    )
