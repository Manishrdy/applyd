"""Build the admin health snapshot.

Single source of truth for both `GET /api/admin/health` (one-shot) and
`GET /api/admin/stream/health` (SSE). Kept tiny on purpose — the SSE
endpoint calls this once per tick, so any work here multiplies.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from app.admin.services import maintenance as maintenance_service
from app.config import settings
from app.database import (
    cached_jobs_total,
    db_reclaimable_bytes,
    get_db,
    last_vacuum_at,
)


def _db_size_bytes() -> int:
    p = settings.db_path
    try:
        return int(p.stat().st_size) if p.exists() else 0
    except OSError:
        return 0


def build_snapshot() -> dict:
    last_vac = last_vacuum_at()
    with get_db() as conn:
        last_ingest = conn.execute(
            "SELECT fetched_at, manifest_updated_at, status "
            "FROM manifest_log WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        scrape_active = conn.execute(
            "SELECT COUNT(*) AS n FROM scrape_run WHERE status IN ('queued','running')"
        ).fetchone()
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "db": {
            "path": str(settings.db_path),
            "size_bytes": _db_size_bytes(),
            "reclaimable_bytes": db_reclaimable_bytes(),
            "last_vacuum_at": last_vac.isoformat() if last_vac else None,
            "jobs_total": cached_jobs_total(),
        },
        "ingestion": {
            "last_success": dict(last_ingest) if last_ingest else None,
            "rolling_window_days": settings.rolling_window_days,
            "cron_hour_utc": settings.ingest_hour_utc,
        },
        "cache": {
            "enabled": settings.redis_cache_enabled,
            "ttl_seconds": settings.redis_cache_ttl_seconds,
        },
        "scrape": {"active_runs": int(scrape_active["n"]) if scrape_active else 0},
        "maintenance": asdict(maintenance_service.get_status()),
    }
