"""CRUD over saved_jobs — the MS2 (auto-apply agent) work queue. Per-user."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.database import get_db
from app.identity.auth import require_user
from app.schemas import (
    SavedJobOut,
    SavedListResponse,
    SavedToggleResponse,
)
from app.services import query as q

router = APIRouter()


VALID_STATUSES = {"queued", "applied", "skipped", "archived"}


class SaveRequest(BaseModel):
    notes: str | None = None
    status: str | None = None


class UpdateSavedRequest(BaseModel):
    notes: str | None = None
    status: str | None = None


@router.get("/", response_model=SavedListResponse)
def list_saved(
    user_id: int = Depends(require_user),
    status: Annotated[str | None, Query(description="queued | applied | skipped | archived")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> SavedListResponse:
    sql = (
        f"SELECT {q.SUMMARY_COLUMNS}, s.saved_at, s.notes, s.status "
        f"FROM saved_jobs s JOIN jobs j ON j.id = s.job_id "
        f"WHERE s.user_id = ?"
    )
    params: list = [user_id]
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(400, f"invalid status; expected one of {sorted(VALID_STATUSES)}")
        sql += " AND s.status = ?"
        params.append(status)
    sql += " ORDER BY s.saved_at DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        count_sql = "SELECT COUNT(*) FROM saved_jobs WHERE user_id = ?"
        count_params: list = [user_id]
        if status:
            count_sql += " AND status = ?"
            count_params.append(status)
        total = conn.execute(count_sql, count_params).fetchone()[0]

    saved_ids = {r["id"] for r in rows}
    job_ids = list(saved_ids)
    reported_ids: set[int] = set()
    if job_ids:
        ph = ",".join(["?"] * len(job_ids))
        with get_db() as conn:
            rep_rows = conn.execute(
                f"SELECT job_id FROM job_reports WHERE user_id = ? AND job_id IN ({ph})",
                [user_id, *job_ids],
            ).fetchall()
        reported_ids = {r["job_id"] for r in rep_rows if r["job_id"] is not None}
    out: list[SavedJobOut] = []
    for r in rows:
        summary = q.row_to_summary(r, saved_ids, reported_ids)
        out.append(SavedJobOut(
            **summary,
            saved_at=r["saved_at"],
            notes=r["notes"],
            status=r["status"] or "queued",
        ))
    return SavedListResponse(saved=out, total=total)


@router.post("/{job_id}", response_model=SavedToggleResponse)
def save_job(
    job_id: int,
    body: SaveRequest | None = None,
    user_id: int = Depends(require_user),
) -> SavedToggleResponse:
    body = body or SaveRequest()
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"invalid status; expected one of {sorted(VALID_STATUSES)}")
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
            raise HTTPException(404, "job not found")
        conn.execute(
            "INSERT INTO saved_jobs (user_id, job_id, notes, status) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, job_id) DO UPDATE SET "
            "notes = COALESCE(excluded.notes, saved_jobs.notes), "
            "status = COALESCE(excluded.status, saved_jobs.status)",
            (user_id, job_id, body.notes, body.status or "queued"),
        )
    return SavedToggleResponse(saved=True, job_id=job_id)


@router.delete("/{job_id}", response_model=SavedToggleResponse)
def unsave_job(
    job_id: int,
    user_id: int = Depends(require_user),
) -> SavedToggleResponse:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM saved_jobs WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        )
    return SavedToggleResponse(saved=False, job_id=job_id)


@router.patch("/{job_id}", response_model=SavedToggleResponse)
def update_saved(
    job_id: int,
    body: UpdateSavedRequest,
    user_id: int = Depends(require_user),
) -> SavedToggleResponse:
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"invalid status; expected one of {sorted(VALID_STATUSES)}")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM saved_jobs WHERE user_id = ? AND job_id = ?",
            (user_id, job_id),
        ).fetchone()
        if not existing:
            raise HTTPException(404, "job is not saved")
        updates = []
        params: list = []
        if body.notes is not None:
            updates.append("notes = ?")
            params.append(body.notes)
        if body.status is not None:
            updates.append("status = ?")
            params.append(body.status)
        if updates:
            params.extend([user_id, job_id])
            conn.execute(
                f"UPDATE saved_jobs SET {', '.join(updates)} WHERE user_id = ? AND job_id = ?",
                params,
            )
    return SavedToggleResponse(saved=True, job_id=job_id)
