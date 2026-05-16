"""Per-user (`/api/me/...`) endpoints: activity stats."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.identity.auth import require_user
from app.schemas import MyStatsResponse
from app.services import user_stats

router = APIRouter()


@router.get("/stats", response_model=MyStatsResponse)
def my_stats(user_id: int = Depends(require_user)) -> MyStatsResponse:
    return user_stats.compute_user_stats(user_id)
