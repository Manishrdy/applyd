"""GET /api/jobs — paginated list + search, /{id}, /facets, /companies."""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import settings
from app.database import get_db
from app.identity.auth import require_user
from app.schemas import (
    CompaniesResponse,
    CompanyHit,
    FacetCount,
    FacetGroup,
    FacetsResponse,
    JobDetail,
    JobSummary,
    JobsListResponse,
)
from app.services import cache
from app.services import query as q

router = APIRouter()

# Map UI-friendly sort keys to the underlying SORT_CLAUSES keys.
SORT_ALIASES = {
    "newest": "newest",
    "oldest": "oldest",
    "salary_high": "salary_high",
    "salary_low": "salary_low",
    "relevance": "relevance",
    # Legacy/alternate names
    "posted_at_desc": "newest",
    "posted_at_asc": "oldest",
}


def _saved_ids_for(conn, user_id: int, job_ids: list[int]) -> set[int]:
    if not job_ids:
        return set()
    ph = ",".join(["?"] * len(job_ids))
    rows = conn.execute(
        f"SELECT job_id FROM saved_jobs WHERE user_id = ? AND job_id IN ({ph})",
        [user_id, *job_ids],
    ).fetchall()
    return {r["job_id"] for r in rows}


def _reported_ids_for(conn, user_id: int, job_ids: list[int]) -> set[int]:
    if not job_ids:
        return set()
    ph = ",".join(["?"] * len(job_ids))
    rows = conn.execute(
        f"SELECT job_id FROM job_reports WHERE user_id = ? AND job_id IN ({ph})",
        [user_id, *job_ids],
    ).fetchall()
    return {r["job_id"] for r in rows if r["job_id"] is not None}


@router.get("/", response_model=JobsListResponse)
def list_jobs(
    user_id: int = Depends(require_user),
    q_: Annotated[str | None, Query(alias="q", description="Full-text search across title/company/description/location")] = None,
    country: Annotated[list[str] | None, Query(description="Country code (e.g. US). Repeatable.")] = None,
    ats: Annotated[list[str] | None, Query(description="ATS type (e.g. greenhouse). Repeatable.")] = None,
    remote: bool | None = None,
    employment_type: Annotated[list[str] | None, Query()] = None,
    department: Annotated[list[str] | None, Query()] = None,
    salary_min_usd: Annotated[int | None, Query(ge=0, description="Minimum annual USD salary")] = None,
    posted_hours: Annotated[int, Query(ge=0, le=720, description="Effective-date window in hours; 0 = no time filter (max 720h = 30d, matches storage)")] = 24,
    include_undated: Annotated[bool, Query(description="Include rows where upstream posted_at is NULL (uses first_seen_at fallback)")] = True,
    company: Annotated[str | None, Query(description="Exact company match (use for drilldown from /companies)")] = None,
    role: Annotated[list[str] | None, Query(description="Curated role key (e.g. backend_engineer). Repeatable. See /api/jobs/roles for the catalog.")] = None,
    seniority: Annotated[list[str] | None, Query(description="Seniority key: junior | mid | senior | staff | principal. Repeatable.")] = None,
    first_seen_after: Annotated[str | None, Query(description="ISO 8601 UTC. Show jobs whose first_seen_at >= this. Use for 'freshly added' (e.g. -6h).")] = None,
    first_seen_before: Annotated[str | None, Query(description="ISO 8601 UTC. Show jobs whose first_seen_at <= this.")] = None,
    updated_after: Annotated[str | None, Query(description="ISO 8601 UTC. Show jobs whose updated_at >= this. Use for 'rows touched by run X' drill-down.")] = None,
    updated_before: Annotated[str | None, Query(description="ISO 8601 UTC. Show jobs whose updated_at <= this.")] = None,
    scrape_run_id: Annotated[int | None, Query(description="Show only the URLs a given manual scrape run loaded. Joins scrape_run_url; immune to later UPSERTs on the same rows.")] = None,
    include_expired: Annotated[bool, Query(description="When True, verified-expired jobs are also returned. Default hides them.")] = False,
    only_expired: Annotated[bool, Query(description="Show ONLY verified-expired jobs (the 'No longer accepting applications' filter category).")] = False,
    sort: Annotated[str, Query(description="newest | oldest | salary_high | salary_low | relevance")] = "newest",
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> JobsListResponse:
    """List jobs with filters. Defaults to USA · last 24h · newest first."""
    limit = min(limit, settings.max_page_size)
    sort_key = SORT_ALIASES.get(sort, "newest")

    cache_payload = {
        "user_id": user_id,
        "q": q_,
        "country": sorted(country or []),
        "ats": sorted(ats or []),
        "remote": remote,
        "employment_type": sorted(employment_type or []),
        "department": sorted(department or []),
        "salary_min_usd": salary_min_usd,
        "posted_hours": posted_hours,
        "include_undated": include_undated,
        "company": company,
        "role": sorted(role or []),
        "seniority": sorted(seniority or []),
        "first_seen_after": first_seen_after,
        "first_seen_before": first_seen_before,
        "updated_after": updated_after,
        "updated_before": updated_before,
        "scrape_run_id": scrape_run_id,
        "include_expired": include_expired,
        "only_expired": only_expired,
        "sort": sort_key,
        "page": page,
        "limit": limit,
    }
    ver = cache.jobs_cache_version()
    cache_key = cache.make_jobs_key(cache_payload, version=ver)
    cached = cache.get_json(cache_key)
    if cached:
        try:
            return JobsListResponse.model_validate_json(cached)
        except Exception:
            pass

    f = q.JobFilters(
        q=q_,
        country=tuple(country or ()),
        ats=tuple(ats or ()),
        remote=remote,
        employment_type=tuple(employment_type or ()),
        department=tuple(department or ()),
        salary_min_usd=salary_min_usd,
        posted_hours=posted_hours,
        include_undated=include_undated,
        company=company,
        roles=tuple(role or ()),
        seniority=tuple(seniority or ()),
        first_seen_after=first_seen_after,
        first_seen_before=first_seen_before,
        updated_after=updated_after,
        updated_before=updated_before,
        scrape_run_id=scrape_run_id,
        include_expired=include_expired,
        only_expired=only_expired,
    )

    offset = (page - 1) * limit
    sql, params = q.build_jobs_select(f, sort=sort_key, limit=limit, offset=offset)
    count_sql, count_params = q.build_count(f)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        total = conn.execute(count_sql, count_params).fetchone()[0]
        job_ids = [r["id"] for r in rows]
        saved_ids = _saved_ids_for(conn, user_id, job_ids)
        reported_ids = _reported_ids_for(conn, user_id, job_ids)

    jobs = [q.row_to_summary(r, saved_ids, reported_ids) for r in rows]
    result = JobsListResponse(
        jobs=[JobSummary(**j) for j in jobs],
        page=page,
        limit=limit,
        total=total,
        has_more=(page * limit) < total,
        sort=sort_key,
    )
    cache.set_json(cache_key, result.model_dump_json(), ttl_seconds=settings.redis_cache_ttl_seconds)
    return result


@router.get("/facets", response_model=FacetsResponse)
def facets(
    q_: Annotated[str | None, Query(alias="q")] = None,
    country: Annotated[list[str] | None, Query()] = None,
    ats: Annotated[list[str] | None, Query()] = None,
    remote: bool | None = None,
    employment_type: Annotated[list[str] | None, Query()] = None,
    department: Annotated[list[str] | None, Query()] = None,
    salary_min_usd: Annotated[int | None, Query(ge=0)] = None,
    posted_hours: Annotated[int, Query(ge=0, le=720)] = 24,
    include_undated: bool = True,
    company: str | None = None,
    role: Annotated[list[str] | None, Query()] = None,
    seniority: Annotated[list[str] | None, Query()] = None,
    first_seen_after: Annotated[str | None, Query()] = None,
    first_seen_before: Annotated[str | None, Query()] = None,
    updated_after: Annotated[str | None, Query()] = None,
    updated_before: Annotated[str | None, Query()] = None,
    scrape_run_id: Annotated[int | None, Query()] = None,
    facets_: Annotated[list[str] | None, Query(alias="facets", description="Which facet groups to compute")] = None,
    limit_per_facet: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FacetsResponse:
    """Faceted counts for the filter sidebar. Each group is computed by
    applying ALL OTHER active filters but ignoring its own — so users see
    "if I checked this, how many would I get" counts."""
    f = q.JobFilters(
        q=q_,
        country=tuple(country or ()),
        ats=tuple(ats or ()),
        remote=remote,
        employment_type=tuple(employment_type or ()),
        department=tuple(department or ()),
        salary_min_usd=salary_min_usd,
        posted_hours=posted_hours,
        include_undated=include_undated,
        company=company,
        roles=tuple(role or ()),
        seniority=tuple(seniority or ()),
        first_seen_after=first_seen_after,
        first_seen_before=first_seen_before,
        updated_after=updated_after,
        updated_before=updated_before,
        scrape_run_id=scrape_run_id,
    )

    requested = [name for name in (facets_ or list(q.FACET_COLUMNS))
                 if name in q.FACET_COLUMNS]

    groups: list[FacetGroup] = []
    with get_db() as conn:
        total = conn.execute(*q.build_count(f)).fetchone()[0]
        for facet_name in requested:
            sql, params = q.build_facet(f, facet_name, limit=limit_per_facet)
            rows = conn.execute(sql, params).fetchall()
            counts = []
            for r in rows:
                value = r["value"]
                if facet_name == "remote" and value is not None:
                    value = bool(value)
                counts.append(FacetCount(value=value, count=r["n"]))
            groups.append(FacetGroup(name=facet_name, counts=counts))

    return FacetsResponse(facets=groups, total_matching=total)


@router.get("/companies", response_model=CompaniesResponse)
def companies(
    q_: Annotated[str | None, Query(alias="q", description="Company name prefix")] = None,
    country: Annotated[list[str] | None, Query()] = None,
    posted_hours: Annotated[int, Query(ge=0, le=720)] = 720,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CompaniesResponse:
    """Top companies by job count. Used for typeahead and the stats page."""
    conditions = ["company IS NOT NULL", "company != ''"]
    params: list = []

    if q_:
        conditions.append("company LIKE ?")
        params.append(f"%{q_}%")

    if country:
        ph = ",".join(["?"] * len(country))
        conditions.append(f"country IN ({ph})")
        params.extend(country)

    if posted_hours and posted_hours > 0:
        conditions.append(
            "COALESCE(posted_at, first_seen_at) >= datetime('now', ?)"
        )
        params.append(f"-{posted_hours} hours")

    where = " AND ".join(conditions)
    sql = (
        f"SELECT company, COUNT(*) AS n FROM jobs WHERE {where} "
        f"GROUP BY company ORDER BY n DESC LIMIT ?"
    )
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return CompaniesResponse(
        companies=[CompanyHit(company=r["company"], count=r["n"]) for r in rows]
    )


CSV_COLUMNS = [
    "id", "title", "company", "location", "country", "is_remote",
    "ats_type", "salary_summary", "salary_min_usd_annual", "salary_max_usd_annual",
    "salary_currency", "employment_type", "department", "team",
    "posted_at", "first_seen_at", "apply_url", "url",
]


@router.get("/export", include_in_schema=False)
def export_csv(
    q_: Annotated[str | None, Query(alias="q")] = None,
    country: Annotated[list[str] | None, Query()] = None,
    ats: Annotated[list[str] | None, Query()] = None,
    remote: bool | None = None,
    employment_type: Annotated[list[str] | None, Query()] = None,
    salary_min_usd: Annotated[int | None, Query(ge=0)] = None,
    posted_hours: Annotated[int, Query(ge=0, le=720)] = 24,
    include_undated: bool = True,
    company: str | None = None,
    sort: str = "newest",
    max_rows: Annotated[int, Query(ge=1, le=10_000)] = 5_000,
) -> StreamingResponse:
    """Streamed CSV export using the same filters as /api/jobs/. Capped at
    `max_rows` to keep the browser download bounded."""
    sort_key = SORT_ALIASES.get(sort, "newest")
    f = q.JobFilters(
        q=q_,
        country=tuple(country or ()),
        ats=tuple(ats or ()),
        remote=remote,
        employment_type=tuple(employment_type or ()),
        department=(),
        salary_min_usd=salary_min_usd,
        posted_hours=posted_hours,
        include_undated=include_undated,
        company=company,
    )
    sql, params = q.build_jobs_select(f, sort=sort_key, limit=max_rows, offset=0)

    def generate():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        with get_db() as conn:
            for row in conn.execute(sql, params):
                rec = {k: row[k] for k in CSV_COLUMNS if k in row.keys()}
                w.writerow(rec)
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)

    headers = {"Content-Disposition": 'attachment; filename="applyd-jobs.csv"'}
    return StreamingResponse(generate(), media_type="text/csv", headers=headers)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(
    job_id: int,
    user_id: int = Depends(require_user),
) -> JobDetail:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {q.DETAIL_COLUMNS} FROM jobs j WHERE j.id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "job not found")
        saved_ids = _saved_ids_for(conn, user_id, [job_id])
        reported_ids = _reported_ids_for(conn, user_id, [job_id])
    return JobDetail(**q.row_to_detail(row, saved_ids, reported_ids))
