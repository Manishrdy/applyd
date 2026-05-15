"""Admin system endpoints — health snapshot + ingestion/VACUUM proxies + SSE.

Covers Phase A1 health pieces (F1/F3/F6/F8), ingestion control (C1/C2),
and the live SSE stream (Feature #6). Thin wrappers over existing
services with admin gating and audit logging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.admin.services.health import build_snapshot
from app.database import vacuum_db
from app.services.ingestion import run_ingestion


log = logging.getLogger(__name__)


router = APIRouter()


# ---- SSE tuning knobs (module-level so tests can monkeypatch) -------------
#
# Stream lifetime is bounded so a stale/forgotten tab doesn't hold a worker
# forever. Heartbeats keep proxies from closing the connection on idle.
STREAM_MAX_SECONDS = 300            # 5 minutes per connection
STREAM_DATA_INTERVAL_SECONDS = 5    # full snapshot every 5s
STREAM_HEARTBEAT_SECONDS = 15       # comment frame between data ticks


@router.get("/health")
def admin_health(admin: AdminUser = Depends(require_admin_user)):
    """One-shot snapshot. The SSE stream below re-uses the same builder."""
    return build_snapshot()


@router.post("/vacuum")
def admin_vacuum(request: Request, admin: AdminUser = Depends(require_admin_user)):
    result = vacuum_db()
    audit.record(admin=admin, action="vacuum_db", detail=result, request=request)
    return result


@router.post("/ingest")
async def admin_ingest(
    request: Request,
    force: bool = Form(False),
    admin: AdminUser = Depends(require_admin_user),
):
    try:
        result = await run_ingestion(force=force)
    except Exception as exc:  # pragma: no cover — surfaces failure shape
        raise HTTPException(status_code=500, detail=str(exc))
    audit.record(
        admin=admin,
        action="manual_ingest",
        detail={"force": force, "status": result.get("status")},
        request=request,
    )
    return result


# ---- SSE live stream ------------------------------------------------------


def _format_event(event: str | None, data: dict | str) -> str:
    """Encode a single SSE frame. `event` is optional; data must end with \\n\\n."""
    body = data if isinstance(data, str) else json.dumps(data, default=str, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    # Each data line must be its own `data: …`; we control the payload shape
    # so one line is sufficient.
    return f"{prefix}data: {body}\n\n"


async def _health_event_stream(request: Request, admin: AdminUser):
    """Yield SSE frames until deadline, client disconnect, or auth expiry.

    Notes for the next reader:
    - `admin` is captured at connect time via the Depends. We don't
      re-verify on every tick — session TTL (days) >> stream lifetime
      (minutes), so the worst case is a single stale tick before the
      max-lifetime timeout forces a reconnect and re-verify.
    - Cancellation: when the client closes the EventSource, Starlette
      cancels the generator. asyncio.sleep() raises CancelledError, which
      we let propagate so the runtime can clean up.
    - We yield a small initial frame *before* the first sleep so the
      browser's `onopen` fires immediately and the UI can render data.
    """
    start = time.monotonic()
    next_data_at = start  # send the first snapshot immediately

    try:
        yield _format_event("hello", {"interval_seconds": STREAM_DATA_INTERVAL_SECONDS})

        while True:
            if await request.is_disconnected():
                return

            now = time.monotonic()
            if now - start >= STREAM_MAX_SECONDS:
                yield _format_event("timeout", {"reason": "max_lifetime_reached"})
                return

            if now >= next_data_at:
                try:
                    snapshot = build_snapshot()
                except Exception:
                    # One bad tick must not kill the stream — log and skip.
                    log.exception("health snapshot failed; skipping tick")
                else:
                    yield _format_event(None, snapshot)
                next_data_at = now + STREAM_DATA_INTERVAL_SECONDS
            else:
                yield ": heartbeat\n\n"

            sleep_for = min(
                STREAM_HEARTBEAT_SECONDS,
                max(0.05, next_data_at - time.monotonic()),
            )
            await asyncio.sleep(sleep_for)
    except asyncio.CancelledError:
        # Client disconnected mid-stream. Re-raise so cleanup runs.
        raise


@router.get("/stream/health")
async def admin_stream_health(
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    return StreamingResponse(
        _health_event_stream(request, admin),
        media_type="text/event-stream",
        headers={
            # Defense against intermediate caches buffering the stream.
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # Nginx-specific hint to disable proxy buffering.
            "X-Accel-Buffering": "no",
        },
    )
