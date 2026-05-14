"""REST + SSE endpoints for the manual local-scraper module.

Trigger -> POST /api/scrape/start          {ats: [...], max_companies_per_ats?: int}
History -> GET  /api/scrape/runs
Detail  -> GET  /api/scrape/runs/{id}
Stream  -> GET  /api/scrape/runs/{id}/stream   (Server-Sent Events; Gate 7 UI consumer)
Cancel  -> POST /api/scrape/runs/{id}/cancel
Logs    -> GET  /api/scrape/runs/{id}/logs/{ats}
Catalog -> GET  /api/scrape/ats               (available ATS + allow-list)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.services import local_scraper, scrape_runner

log = logging.getLogger(__name__)
router = APIRouter()


class StartRequest(BaseModel):
    # max_length enforced at start_run() against settings.local_scraper_max_ats_per_run
    # so the UI and the API share one source of truth.
    ats: list[str] = Field(..., min_length=1)
    max_companies_per_ats: int | None = Field(
        default=None, ge=1, le=100_000,
        description="Bound per-ATS. None falls back to settings.local_scraper_default_max_companies.",
    )
    triggered_by: Literal["manual_ui", "manual_api", "cli"] = "manual_api"
    incremental_enabled: bool = False
    incremental_days: int | None = Field(default=None, ge=1, le=365)
    preset_id: int | None = Field(default=None, ge=1)


class PresetPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    ats_requested: list[str] = Field(..., min_length=1)
    max_companies_per_ats: int | None = Field(default=None, ge=1, le=100_000)
    incremental_enabled: bool = False
    incremental_days: int | None = Field(default=None, ge=1, le=365)
    notes: str | None = Field(default=None, max_length=500)
    is_default: bool = False


@router.get("/ats")
def list_ats() -> dict:
    if not settings.local_scraper_enabled:
        raise HTTPException(503, "local scraper disabled in settings")
    available = local_scraper.available_ats()
    # Empty allow-list in config = "no restriction": surface every available
    # ATS as allowed so the UI doesn't grey them out.
    cfg_allowed = list(settings.local_scraper_allowed_ats)
    allowed = cfg_allowed if cfg_allowed else available
    return {
        "available": available,
        "allowed": allowed,
        "recommended": list(settings.local_scraper_recommended_ats),
        "max_ats_per_run": settings.local_scraper_max_ats_per_run,
        "vendor_commit": local_scraper.vendor_commit_sha(),
        "default_max_companies": settings.local_scraper_default_max_companies,
        "default_incremental_days": settings.local_scraper_default_incremental_days,
        "timeout_seconds": settings.local_scraper_timeout_seconds,
        "supports_rotation": True,
        "supports_incremental": True,
    }


@router.post("/start", status_code=202)
async def start(req: StartRequest) -> dict:
    if not settings.local_scraper_enabled:
        raise HTTPException(503, "local scraper disabled in settings")

    ats_list = req.ats
    max_companies = req.max_companies_per_ats
    incremental_enabled = req.incremental_enabled
    incremental_days = req.incremental_days
    if req.preset_id is not None:
        try:
            preset = scrape_runner.get_preset(req.preset_id)
        except ValueError as e:
            raise HTTPException(404, str(e))
        ats_list = preset["ats_requested"]
        max_companies = preset.get("max_companies_per_ats")
        incremental_enabled = bool(preset.get("incremental_enabled"))
        incremental_days = preset.get("incremental_days")

    if max_companies is None:
        max_companies = settings.local_scraper_default_max_companies
    if incremental_enabled and incremental_days is None:
        incremental_days = settings.local_scraper_default_incremental_days

    try:
        run_id = await scrape_runner.start_run(
            ats_list,
            triggered_by=req.triggered_by,
            max_companies_per_ats=max_companies,
            incremental_enabled=incremental_enabled,
            incremental_days=incremental_days,
            preset_id=req.preset_id,
        )
    except scrape_runner.SingleFlightError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "run_id": run_id,
        "status_url": f"/api/scrape/runs/{run_id}",
        "stream_url": f"/api/scrape/runs/{run_id}/stream",
    }


@router.get("/runs")
def runs(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict:
    return {"runs": scrape_runner.list_runs(limit=limit, offset=offset)}


@router.get("/presets")
def list_presets() -> dict:
    return {"presets": scrape_runner.list_presets()}


@router.post("/presets")
def create_preset(req: PresetPayload) -> dict:
    try:
        preset = scrape_runner.create_preset(**req.model_dump())
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"preset": preset}


@router.put("/presets/{preset_id}")
def update_preset(preset_id: int, req: PresetPayload) -> dict:
    try:
        preset = scrape_runner.update_preset(preset_id, **req.model_dump())
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"preset": preset}


@router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(preset_id: int) -> Response:
    scrape_runner.delete_preset(preset_id)
    return Response(status_code=204)


@router.get("/coverage")
def coverage(ats: list[str] = Query(default=[])) -> dict:
    targets = ats if ats else None
    return {"coverage": scrape_runner.coverage_summary(targets)}


@router.get("/coverage/{ats}")
def coverage_detail(ats: str, limit: int = Query(300, ge=1, le=2000)) -> dict:
    return {"rows": scrape_runner.coverage_detail(ats, limit=limit)}


@router.get("/runs/{run_id}")
def run_detail(run_id: int) -> dict:
    detail = scrape_runner.get_run(run_id)
    if detail is None:
        raise HTTPException(404, f"run {run_id} not found")
    return detail


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: int) -> dict:
    detail = scrape_runner.get_run(run_id)
    if detail is None:
        raise HTTPException(404, f"run {run_id} not found")
    if detail["status"] not in ("queued", "running"):
        raise HTTPException(409, f"run {run_id} is {detail['status']}, not cancelable")
    ok = await scrape_runner.cancel_run(run_id)
    if not ok:
        raise HTTPException(409, "run is no longer active in this process")
    return {"run_id": run_id, "cancel_requested": True}


@router.get("/runs/{run_id}/logs/{ats}", response_class=PlainTextResponse)
def logs(run_id: int, ats: str, tail_lines: int = Query(2000, ge=1, le=100_000)) -> str:
    p = scrape_runner.get_log_path(run_id, ats)
    if p is None:
        raise HTTPException(404, f"no log for run {run_id} ats {ats}")
    text = p.read_text(errors="replace").splitlines()
    return "\n".join(text[-tail_lines:])


# ---- SSE live stream -----------------------------------------------------


def _sse_format(event: dict) -> bytes:
    """Encode one event as a Server-Sent Events frame."""
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


@router.get("/runs/{run_id}/stream")
async def stream(run_id: int):
    """SSE stream of progress events for a live run. Replays the current
    per-ATS snapshot on connect, then streams new events as they happen.
    Disconnects cleanly when the run finishes."""
    handle = scrape_runner.get_run_handle(run_id)
    if handle is None:
        # The run may already be finished — surface the final state once and exit.
        detail = scrape_runner.get_run(run_id)
        if detail is None:
            raise HTTPException(404, f"run {run_id} not found")

        async def replay_once():
            yield _sse_format({"event": "run_already_finished", **detail})
        return StreamingResponse(replay_once(), media_type="text/event-stream")

    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    handle.subscribers.append(q)

    async def gen():
        try:
            # Replay current per-ATS snapshot so a late subscriber catches up.
            for ats, prog in handle.per_ats.items():
                yield _sse_format({
                    "event": "ats_progress_snapshot",
                    "run_id": run_id,
                    "ats": ats,
                    "companies_total": prog.companies_total,
                    "companies_succeeded": prog.companies_succeeded,
                    "companies_failed": prog.companies_failed,
                    "rows_scraped": prog.rows_scraped,
                    "rows_written": prog.rows_written,
                    "rows_inserted": prog.rows_inserted,
                    "rows_updated": prog.rows_updated,
                    "phase": prog.phase,
                    "phase_started_at": prog.phase_started_at,
                    "eta_seconds": prog.eta_seconds,
                    "throughput_cpm": prog.throughput_cpm,
                })
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=20)
                    yield _sse_format(evt)
                    if evt.get("event") == "run_finished":
                        return
                except asyncio.TimeoutError:
                    # heartbeat keeps the connection open through proxies
                    yield b": heartbeat\n\n"
        finally:
            try:
                handle.subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )
