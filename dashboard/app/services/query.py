"""SQL query builder for the jobs/facets/stats endpoints.

Two-track architecture:
  - When `q` is set, queries go through jobs_fts and JOIN to jobs (FTS5 ranking
    available via f.rank).
  - When `q` is None, queries go directly against jobs (no JOIN).

All time-window filters use COALESCE(posted_at, first_seen_at) — see
[[project-data-decisions]] for the synthetic-date rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# Allowlisted facet columns — passed into SQL string interpolation, so they
# MUST be validated against this set before any query.
FACET_COLUMNS: dict[str, str] = {
    "country": "country",
    "ats": "ats_type",
    "employment_type": "employment_type",
    "remote": "is_remote",
    "department": "department",
}

# Allowlisted sort modes. Sort key → ORDER BY clause.
# `relevance` only valid when q is set (uses FTS rank); we fall back if q is None.
# Note: FTS5's MATCH and rank require referencing the FTS table by its full
# name (jobs_fts), not an alias — that's why we don't alias it below.
SORT_CLAUSES: dict[str, str] = {
    "newest": "COALESCE(j.posted_at, j.first_seen_at) DESC",
    "oldest": "COALESCE(j.posted_at, j.first_seen_at) ASC",
    "salary_high": "j.salary_max_usd_annual DESC NULLS LAST",
    "salary_low": "j.salary_min_usd_annual ASC NULLS LAST",
    "relevance": "jobs_fts.rank",
}

DEFAULT_SORT = "newest"

# Light column set for list views — drops the heavy `description`.
SUMMARY_COLUMNS = (
    "j.id, j.url, j.title, j.company, j.location, j.country, j.ats_type, "
    "j.is_remote, j.posted_at, j.first_seen_at, j.salary_summary, "
    "j.salary_min_usd_annual, j.salary_max_usd_annual, j.salary_currency, "
    "j.employment_type, j.department, j.apply_url"
)

DETAIL_COLUMNS = (
    SUMMARY_COLUMNS
    + ", j.description, j.requisition_id, j.salary_min, j.salary_max, "
    "j.salary_period, j.team, j.commitment, j.fetched_cycle, j.updated_at"
)


@dataclass(frozen=True)
class JobFilters:
    q: str | None = None
    country: tuple[str, ...] = ()
    ats: tuple[str, ...] = ()
    remote: bool | None = None
    employment_type: tuple[str, ...] = ()
    department: tuple[str, ...] = ()
    salary_min_usd: int | None = None
    posted_hours: int | None = 24
    include_undated: bool = True
    company: str | None = None

    def with_overrides(self, **kw) -> "JobFilters":
        return replace(self, **kw)


_FTS_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-\s]")


def sanitize_fts(q: str) -> str:
    """Strip anything that could confuse the FTS5 parser. Returns a query
    string safe to pass as a positional parameter to a `MATCH ?` clause.

    Strategy: keep alphanumerics, underscores, hyphens, and whitespace.
    Wrap each token in double quotes so phrase/AND semantics stay predictable.
    Empty input → empty string (caller should treat as "no FTS").
    """
    cleaned = _FTS_SAFE_RE.sub(" ", q or "").strip()
    if not cleaned:
        return ""
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return ""
    return " ".join(f'"{t}"' for t in tokens)


def _build_where(
    f: JobFilters, *, skip: str | None = None
) -> tuple[list[str], list]:
    """Return (conditions, params). `skip` excludes one facet's filter
    from the WHERE — used by the facets endpoint to compute counts that
    react to OTHER active filters but not the one being counted.
    """
    conditions: list[str] = []
    params: list = []

    if f.country and skip != "country":
        ph = ",".join(["?"] * len(f.country))
        conditions.append(f"j.country IN ({ph})")
        params.extend(f.country)

    if f.ats and skip != "ats":
        ph = ",".join(["?"] * len(f.ats))
        conditions.append(f"j.ats_type IN ({ph})")
        params.extend(f.ats)

    if f.remote is not None and skip != "remote":
        conditions.append("j.is_remote = ?")
        params.append(1 if f.remote else 0)

    if f.employment_type and skip != "employment_type":
        ph = ",".join(["?"] * len(f.employment_type))
        conditions.append(f"j.employment_type IN ({ph})")
        params.extend(f.employment_type)

    if f.department and skip != "department":
        ph = ",".join(["?"] * len(f.department))
        conditions.append(f"j.department IN ({ph})")
        params.extend(f.department)

    if f.salary_min_usd is not None:
        # Match if either bound clears the user's floor.
        conditions.append(
            "(j.salary_min_usd_annual >= ? OR j.salary_max_usd_annual >= ?)"
        )
        params.extend([f.salary_min_usd, f.salary_min_usd])

    if f.posted_hours and f.posted_hours > 0:
        conditions.append(
            "COALESCE(j.posted_at, j.first_seen_at) >= datetime('now', ?)"
        )
        params.append(f"-{f.posted_hours} hours")

    # Always cap the effective date at "now" — a non-trivial number of upstream
    # rows (Workday especially) have `posted_at` set to a future date like
    # 2027-08-01, which would otherwise pollute every time-window query
    # ("posted in the last 24h" matches them too because they're > any past
    # cutoff). Filter at query time so the data stays available if we ever
    # want to expose it via an explicit `?include_future=true` later.
    conditions.append("COALESCE(j.posted_at, j.first_seen_at) <= datetime('now')")

    if not f.include_undated:
        conditions.append("j.posted_at IS NOT NULL")

    if f.company:
        conditions.append("j.company = ?")
        params.append(f.company)

    return conditions, params


# Cap for FTS-leading subquery — top-N matches by rank or rowid before joining.
# 5000 covers any realistic paging depth (max 100/page × 50 pages).
FTS_PREFILTER_CAP = 5000


def _fts_prefilter_sql(rank_sort: bool) -> str:
    """Inner subquery: SELECT rowid (and rank) from jobs_fts WHERE MATCH ?.

    SQLite's planner otherwise picks country_eff or another index as leading
    and probes FTS once per candidate row — catastrophic on 500K candidates.
    Forcing FTS to lead via subquery reduces that to one FTS scan + N point
    lookups, where N = matches (typically 10–50K).
    """
    if rank_sort:
        return (
            "SELECT rowid, rank FROM jobs_fts WHERE jobs_fts MATCH ? "
            f"ORDER BY rank LIMIT {FTS_PREFILTER_CAP}"
        )
    return (
        "SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ? "
        f"LIMIT {FTS_PREFILTER_CAP}"
    )


def build_jobs_select(
    f: JobFilters,
    *,
    detail: bool = False,
    sort: str = DEFAULT_SORT,
    limit: int = 50,
    offset: int = 0,
) -> tuple[str, list]:
    cols = DETAIL_COLUMNS if detail else SUMMARY_COLUMNS

    sort_key = sort if sort in SORT_CLAUSES else DEFAULT_SORT
    if sort_key == "relevance" and not f.q:
        sort_key = DEFAULT_SORT

    conditions, where_params = _build_where(f)
    where_extra = " AND " + " AND ".join(conditions) if conditions else ""

    if f.q and (fts_q := sanitize_fts(f.q)):
        # FTS-first via materialized subquery. For relevance sort we keep
        # the rank inside the subquery so we can ORDER BY it in the outer.
        rank_sort = sort_key == "relevance"
        inner = _fts_prefilter_sql(rank_sort)
        params: list = [fts_q]
        if rank_sort:
            sql = (
                f"SELECT {cols} FROM ({inner}) f "
                f"JOIN jobs j ON j.id = f.rowid "
                f"WHERE 1=1{where_extra} "
                f"ORDER BY f.rank LIMIT ? OFFSET ?"
            )
        else:
            sql = (
                f"SELECT {cols} FROM jobs j "
                f"WHERE j.id IN ({inner}){where_extra} "
                f"ORDER BY {SORT_CLAUSES[sort_key]} LIMIT ? OFFSET ?"
            )
        params.extend(where_params)
        params.extend([limit, offset])
        return sql, params

    # No FTS — straight scan with indexes.
    sql = (
        f"SELECT {cols} FROM jobs j WHERE 1=1{where_extra} "
        f"ORDER BY {SORT_CLAUSES[sort_key]} LIMIT ? OFFSET ?"
    )
    params = where_params + [limit, offset]
    return sql, params


def build_count(f: JobFilters, *, skip: str | None = None) -> tuple[str, list]:
    """COUNT(*) for the same filter set as build_jobs_select. FTS-first when
    `q` is set so SQLite doesn't probe FTS once per candidate row."""
    conditions, where_params = _build_where(f, skip=skip)

    if f.q and (fts_q := sanitize_fts(f.q)):
        inner = _fts_prefilter_sql(rank_sort=False)
        sql = f"SELECT COUNT(*) FROM jobs j WHERE j.id IN ({inner})"
        params: list = [fts_q]
        if conditions:
            sql += " AND " + " AND ".join(conditions)
            params.extend(where_params)
        return sql, params

    where_extra = " WHERE " + " AND ".join(conditions) if conditions else ""
    return f"SELECT COUNT(*) FROM jobs j{where_extra}", where_params


def build_facet(
    f: JobFilters, facet: str, *, limit: int = 50
) -> tuple[str, list]:
    """Aggregate one facet group. FTS-first when `q` is set, same reason as build_count."""
    if facet not in FACET_COLUMNS:
        raise ValueError(f"unknown facet: {facet}")
    column = FACET_COLUMNS[facet]
    conditions, where_params = _build_where(f, skip=facet)

    if f.q and (fts_q := sanitize_fts(f.q)):
        inner = _fts_prefilter_sql(rank_sort=False)
        sql = (
            f"SELECT j.{column} AS value, COUNT(*) AS n "
            f"FROM jobs j WHERE j.id IN ({inner})"
        )
        params: list = [fts_q]
        if conditions:
            sql += " AND " + " AND ".join(conditions)
            params.extend(where_params)
        sql += f" GROUP BY j.{column} ORDER BY n DESC LIMIT ?"
        params.append(limit)
        return sql, params

    where_extra = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = (
        f"SELECT j.{column} AS value, COUNT(*) AS n FROM jobs j{where_extra} "
        f"GROUP BY j.{column} ORDER BY n DESC LIMIT ?"
    )
    return sql, where_params + [limit]


def row_to_summary(row, saved_ids: set[int] | None = None) -> dict:
    """Convert a SQLite Row to a dict matching JobSummary."""
    posted = row["posted_at"]
    first = row["first_seen_at"]
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "country": row["country"],
        "ats_type": row["ats_type"],
        "is_remote": bool(row["is_remote"]) if row["is_remote"] is not None else None,
        "posted_at": posted,
        "first_seen_at": first,
        "effective_date": posted or first,
        "is_dated": posted is not None,
        "salary_summary": row["salary_summary"],
        "salary_min_usd_annual": row["salary_min_usd_annual"],
        "salary_max_usd_annual": row["salary_max_usd_annual"],
        "salary_currency": row["salary_currency"],
        "employment_type": row["employment_type"],
        "department": row["department"],
        "apply_url": row["apply_url"],
        "is_saved": row["id"] in saved_ids if saved_ids is not None else False,
    }


def row_to_detail(row, saved_ids: set[int] | None = None) -> dict:
    base = row_to_summary(row, saved_ids)
    base.update({
        "description": row["description"],
        "requisition_id": row["requisition_id"],
        "salary_min": row["salary_min"],
        "salary_max": row["salary_max"],
        "salary_period": row["salary_period"],
        "team": row["team"],
        "commitment": row["commitment"],
        "fetched_cycle": row["fetched_cycle"],
        "updated_at": row["updated_at"],
    })
    return base
