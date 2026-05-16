"""Pydantic response models for API endpoints.

Request validation comes from typed FastAPI route signatures, not these models —
these are response-only (used in `response_model=` to control output shape and
generate /docs entries).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JobSummary(BaseModel):
    """Light job fields for list/grid views — no description."""
    id: int
    url: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    country: str | None = None
    ats_type: str | None = None
    is_remote: bool | None = None
    posted_at: str | None = None
    first_seen_at: str | None = None
    effective_date: str | None = Field(
        None,
        description="COALESCE(posted_at, first_seen_at) — what the dashboard sorts/filters by",
    )
    is_dated: bool = Field(
        False,
        description="True if posted_at is set (real upstream date); False if first_seen_at is the proxy",
    )
    salary_summary: str | None = None
    salary_min_usd_annual: float | None = None
    salary_max_usd_annual: float | None = None
    salary_currency: str | None = None
    employment_type: str | None = None
    department: str | None = None
    apply_url: str | None = None
    is_saved: bool = False
    verification_status: str = Field(
        "active",
        description="active | suspected | expired — see services/job_lifecycle.py",
    )
    is_reported: bool = False


class JobDetail(JobSummary):
    """Full job with description, for the detail page."""
    description: str | None = None
    requisition_id: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None
    team: str | None = None
    commitment: str | None = None
    fetched_cycle: str | None = None
    updated_at: str | None = None


class JobsListResponse(BaseModel):
    jobs: list[JobSummary]
    page: int
    limit: int
    total: int
    has_more: bool
    sort: str


class FacetCount(BaseModel):
    value: str | bool | None
    count: int
    label: str | None = None


class FacetGroup(BaseModel):
    name: str
    counts: list[FacetCount]


class FacetsResponse(BaseModel):
    facets: list[FacetGroup]
    total_matching: int


class CompanyHit(BaseModel):
    company: str
    count: int


class CompaniesResponse(BaseModel):
    companies: list[CompanyHit]


class SavedJobOut(JobSummary):
    saved_at: str
    notes: str | None = None
    status: str = "queued"


class SavedListResponse(BaseModel):
    saved: list[SavedJobOut]
    total: int


class SavedToggleResponse(BaseModel):
    saved: bool
    job_id: int


class StatsSummary(BaseModel):
    total_jobs: int
    dated: int
    undated: int
    us_total: int
    us_24h: int
    us_7d: int
    us_30d: int
    remote: int
    ats_count: int
    company_count: int
    last_sync: str | None = None
    rolling_window_days: int


class CountByLabel(BaseModel):
    label: str
    count: int


class GroupedCounts(BaseModel):
    items: list[CountByLabel]


class SalaryBucket(BaseModel):
    low: int
    high: int | None
    count: int


class SalaryRangeResponse(BaseModel):
    buckets: list[SalaryBucket]
    median: float | None
    p25: float | None
    p75: float | None


class RemoteVsOnsite(BaseModel):
    remote: int
    onsite: int
    unknown: int


# ---- Per-user activity stats (/api/me/stats) -------------------------------


class ByStatusCounts(BaseModel):
    queued: int
    applied: int
    skipped: int
    archived: int


class SavesPerDayPoint(BaseModel):
    date: str  # YYYY-MM-DD
    count: int


class TopCount(BaseModel):
    key: str
    count: int


class MyStatsResponse(BaseModel):
    total_saved: int
    by_status: ByStatusCounts
    saves_per_day: list[SavesPerDayPoint]
    top_companies: list[TopCount]
    top_ats: list[TopCount]
    conversion_rate: float
