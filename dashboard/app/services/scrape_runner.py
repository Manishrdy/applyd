"""Scrape run orchestrator: claims a DB-level single-flight slot, runs the
LocalScraperSource for each requested ATS sequentially, threads progress
events through an in-process registry so the API/SSE layer can stream them
live, and handles cancellation + crash recovery.

This is the surface the /api/scrape/* router talks to. The actual scraper
subprocess is owned by services/local_scraper.run_one_ats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.database import get_db
from app.services import local_scraper

log = logging.getLogger(__name__)

_LOG_ROOT = Path(__file__).resolve().parents[2] / "logs" / "scraper"
_PARQUET_ROOT = Path(__file__).resolve().parents[2] / "data" / "by_ats"

# in-process registry: run_id -> RunHandle. Populated when a run starts,
# removed when it finishes. SSE consumers attach to a handle to receive
# live progress; readers fall back to DB if the run is no longer in memory.
_runs: dict[int, "RunHandle"] = {}
_runs_lock = asyncio.Lock()


@dataclass
class RunHandle:
    run_id: int
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # latest ScrapeProgress per ATS (in-memory mirror of scrape_run_ats counters)
    per_ats: dict[str, local_scraper.ScrapeProgress] = field(default_factory=dict)
    # SSE subscriber queues; producer broadcasts every progress event to all.
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    task: asyncio.Task | None = None
    incremental_enabled: bool = False
    incremental_days: int | None = None

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    async def broadcast(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


# ---- crash recovery (called once on app startup) -------------------------


def cleanup_orphans() -> int:
    """Mark any scrape_run rows left in queued/running state by a previous
    process as failed. Same for their scrape_run_ats children. Returns count
    of runs reclaimed."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id FROM scrape_run WHERE status IN ('queued', 'running')"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE scrape_run SET status='failed', finished_at=?, "
            f"error='orphaned_by_restart' WHERE id IN ({q})",
            (now, *ids),
        )
        conn.execute(
            f"UPDATE scrape_run_ats SET status='failed', finished_at=?, "
            f"error='orphaned_by_restart' "
            f"WHERE run_id IN ({q}) AND status IN ('pending', 'running')",
            (now, *ids),
        )
    log.info("cleanup_orphans: reclaimed %d orphaned run(s)", len(ids))
    return len(ids)


def cleanup_retention() -> dict[str, int]:
    """Apply retention policies on startup:
      - Delete scrape_run rows beyond settings.scrape_run_history_keep newest
        (CASCADE drops their scrape_run_ats children).
      - Delete log files older than settings.scraper_log_retention_days.
    Returns counts for logging.
    """
    runs_dropped = 0
    logs_dropped = 0
    try:
        with get_db() as conn:
            keep = max(int(settings.scrape_run_history_keep), 1)
            cur = conn.execute(
                "DELETE FROM scrape_run WHERE id NOT IN "
                "(SELECT id FROM scrape_run ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            runs_dropped = cur.rowcount or 0
    except sqlite3.OperationalError as exc:
        # Startup should not fail just because a concurrent writer briefly
        # holds the DB lock; retention can run successfully on a later boot.
        if "locked" in str(exc).lower():
            log.warning("cleanup_retention: skipped DB prune due to lock: %s", exc)
        else:
            raise

    if _LOG_ROOT.exists():
        import time
        cutoff = time.time() - (settings.scraper_log_retention_days * 86_400)
        for p in _LOG_ROOT.glob("run-*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    logs_dropped += 1
            except OSError:
                pass

    if runs_dropped or logs_dropped:
        log.info("cleanup_retention: dropped %d run rows, %d log files",
                 runs_dropped, logs_dropped)
    return {"runs_dropped": runs_dropped, "logs_dropped": logs_dropped}


# ---- starting a run ------------------------------------------------------


class SingleFlightError(Exception):
    """Raised when another run is already queued or running."""


def _claim_run(
    ats_list: list[str],
    triggered_by: str,
    max_companies_per_ats: int | None,
    *,
    incremental_enabled: bool = False,
    incremental_days: int | None = None,
    preset_id: int | None = None,
) -> int:
    """Insert a queued scrape_run row + per-ATS pending rows. The partial
    unique index on (status IN queued/running) enforces single-flight at the
    DB level — a second concurrent claim raises IntegrityError, which we
    translate to SingleFlightError.

    An app-layer pre-check runs first as defense in depth (and to produce a
    clearer error before the IntegrityError path is hit).
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        active = conn.execute(
            "SELECT id FROM scrape_run WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        if active is not None:
            raise SingleFlightError(
                f"another scrape run is already active (run {active['id']})"
            )
        try:
            cur = conn.execute(
                "INSERT INTO scrape_run "
                "(started_at, status, ats_requested, triggered_by, "
                " scraper_version, max_companies_per_ats, incremental_enabled, "
                " incremental_days, preset_id) "
                "VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                (now, json.dumps(ats_list), triggered_by,
                 local_scraper.vendor_commit_sha(), max_companies_per_ats,
                 1 if incremental_enabled else 0, incremental_days, preset_id),
            )
        except sqlite3.IntegrityError:
            # The partial unique index caught a race the pre-check didn't.
            raise SingleFlightError("another scrape run is already active")
        run_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO scrape_run_ats (run_id, ats, status) VALUES (?, ?, 'pending')",
            [(run_id, ats) for ats in ats_list],
        )
    return run_id


def _set_run_status(run_id: int, status: str, *,
                    error: str | None = None,
                    finished: bool = False,
                    totals: dict[str, int] | None = None) -> None:
    sets = ["status=?"]
    args: list[Any] = [status]
    if error is not None:
        sets.append("error=?")
        args.append(error)
    if finished:
        sets.append("finished_at=?")
        args.append(datetime.now(timezone.utc).isoformat())
    if totals:
        for k, v in totals.items():
            sets.append(f"{k}=?")
            args.append(v)
    args.append(run_id)
    with get_db() as conn:
        conn.execute(
            f"UPDATE scrape_run SET {', '.join(sets)} WHERE id=?", args
        )


def _set_ats_status(run_id: int, ats: str, status: str, *,
                    started: bool = False,
                    finished: bool = False,
                    error: str | None = None,
                    log_path: Path | None = None,
                    progress: local_scraper.ScrapeProgress | None = None,
                    load_result: local_scraper.LoadResult | None = None) -> None:
    sets = ["status=?"]
    args: list[Any] = [status]
    if started:
        sets.append("started_at=?")
        args.append(datetime.now(timezone.utc).isoformat())
    if finished:
        sets.append("finished_at=?")
        args.append(datetime.now(timezone.utc).isoformat())
    if error is not None:
        sets.append("error=?")
        args.append(error[:500])
    if log_path is not None:
        sets.append("log_path=?")
        args.append(str(log_path))
    if progress is not None:
        sets += [
            "companies_total=?", "companies_succeeded=?", "companies_failed=?",
            "rows_scraped=?",
        ]
        args += [
            progress.companies_total, progress.companies_succeeded,
            progress.companies_failed, progress.rows_scraped,
        ]
    if load_result is not None:
        sets += ["rows_written=?", "rows_skipped_safeguard=?"]
        args += [load_result.rows_written, load_result.rows_skipped_safeguard]
        sets += ["rows_inserted=?", "rows_updated=?"]
        args += [load_result.rows_inserted, load_result.rows_updated]
    if progress is not None:
        sets += ["selected_companies=?", "phase=?", "phase_started_at=?", "eta_seconds=?", "throughput_cpm=?"]
        args += [
            progress.selected_companies, progress.phase, progress.phase_started_at,
            progress.eta_seconds, progress.throughput_cpm
        ]
    args += [run_id, ats]
    with get_db() as conn:
        conn.execute(
            f"UPDATE scrape_run_ats SET {', '.join(sets)} "
            f"WHERE run_id=? AND ats=?", args
        )


def _snapshot_run_urls(run_id: int, ats: str, parquet_path: Path) -> None:
    """Insert this run's URL set into scrape_run_url so the per-ATS drill-down
    can recover the exact rows even after later writes (cron, other runs) bump
    jobs.updated_at on the same URLs. Cheap: one column read + executemany.
    INSERT OR IGNORE guards against the same URL appearing twice in the
    parquet (unlikely but cheap to defend)."""
    try:
        import pyarrow.parquet as pq
        urls = pq.read_table(str(parquet_path), columns=["url"]).column("url").to_pylist()
    except Exception:
        log.exception("[run=%d %s] could not snapshot URLs from %s",
                      run_id, ats, parquet_path)
        return
    urls = [u for u in urls if u]
    if not urls:
        return
    with get_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO scrape_run_url(run_id, ats, url) VALUES (?, ?, ?)",
            [(run_id, ats, u) for u in urls],
        )
    log.info("[run=%d %s] snapshotted %d URLs into scrape_run_url",
             run_id, ats, len(urls))


def _aggregate_totals(run_id: int) -> dict[str, int]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(rows_scraped),0) AS s, "
            "       COALESCE(SUM(companies_failed),0) AS f, "
            "       COALESCE(SUM(rows_written),0) AS w, "
            "       COALESCE(SUM(rows_inserted),0) AS i, "
            "       COALESCE(SUM(rows_updated),0) AS u "
            "FROM scrape_run_ats WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return {
        "total_scraped": int(row["s"]),
        "total_failed": int(row["f"]),
        "total_written": int(row["w"]),
        "total_inserted": int(row["i"]),
        "total_updated": int(row["u"]),
    }


# ---- the orchestrator ----------------------------------------------------


async def _orchestrate(run_id: int, handle: RunHandle, ats_list: list[str],
                       max_companies_per_ats: int | None) -> None:
    """The background task that runs after _claim_run. Sequential per ATS;
    a failure in one ATS does NOT abort the run. Top-level errors are
    surfaced to scrape_run.error so they don't get swallowed as silent
    task failures."""
    try:
        await _orchestrate_inner(run_id, handle, ats_list, max_companies_per_ats)
    except Exception as e:
        log.exception("orchestrator for run %d crashed", run_id)
        try:
            _set_run_status(run_id, "failed", finished=True,
                            error=f"orchestrator_crashed: {type(e).__name__}: {e}")
        except Exception:
            log.exception("could not even mark run %d as failed", run_id)


async def _orchestrate_inner(run_id: int, handle: RunHandle, ats_list: list[str],
                             max_companies_per_ats: int | None) -> None:
    _set_run_status(run_id, "running")
    await handle.broadcast({"event": "run_running", "run_id": run_id})

    any_succeeded = False
    any_failed = False
    any_safeguard = False

    for ats in ats_list:
        if handle.cancelled():
            _set_ats_status(run_id, ats, "cancelled", finished=True,
                            error="cancelled_before_start")
            any_failed = True
            continue

        parquet_path = _PARQUET_ROOT / f"{ats}.parquet"
        log_path = _LOG_ROOT / f"run-{run_id}-{ats}.log"

        _set_ats_status(run_id, ats, "running", started=True, log_path=log_path)
        await handle.broadcast({"event": "ats_running", "run_id": run_id, "ats": ats, "phase": "selecting"})

        selected_companies = local_scraper.select_companies_for_run(
            ats,
            max_companies=max_companies_per_ats,
            incremental_enabled=handle.incremental_enabled,
            incremental_days=handle.incremental_days,
        )
        selected_slugs = [c.slug for c in selected_companies]
        seed_progress = local_scraper.ScrapeProgress(
            ats=ats,
            selected_companies=len(selected_slugs),
            companies_total=len(selected_slugs),
            phase="selecting",
            phase_started_at=datetime.now(timezone.utc).isoformat(),
        )
        _set_ats_status(run_id, ats, "running", progress=seed_progress)

        async def on_progress(p: local_scraper.ScrapeProgress, _ats=ats) -> None:
            handle.per_ats[_ats] = p
            # Persist counters to DB so disconnected SSE clients can still
            # see progress on reload (modest write rate — events are line-
            # per-company, so ~1 write per company per ATS).
            _set_ats_status(run_id, _ats, "running", progress=p)
            await handle.broadcast({
                "event": "ats_progress",
                "run_id": run_id,
                "ats": _ats,
                "companies_total": p.companies_total,
                "companies_succeeded": p.companies_succeeded,
                "companies_failed": p.companies_failed,
                "rows_scraped": p.rows_scraped,
                "rows_written": p.rows_written,
                "rows_inserted": p.rows_inserted,
                "rows_updated": p.rows_updated,
                "phase": p.phase,
                "phase_started_at": p.phase_started_at,
                "eta_seconds": p.eta_seconds,
                "throughput_cpm": p.throughput_cpm,
                "last_event": p.last_event,
            })

        try:
            progress = await local_scraper.run_one_ats(
                ats,
                parquet_path,
                log_path,
                max_companies=max_companies_per_ats,
                timeout_seconds=settings.local_scraper_timeout_seconds,
                on_progress=on_progress,
                cancel_event=handle.cancel_event,
                per_company_concurrency=settings.local_scraper_per_company_concurrency,
                selected_slugs=selected_slugs,
                run_id=run_id,
            )
        except local_scraper.ScrapeCancelled:
            log.info("[run=%d %s] cancelled mid-run", run_id, ats)
            partial = handle.per_ats.get(ats)
            _set_ats_status(run_id, ats, "cancelled", finished=True,
                            error="cancelled_mid_run",
                            progress=partial)
            any_failed = True
            continue
        except asyncio.TimeoutError:
            log.warning("[run=%d %s] timed out", run_id, ats)
            _set_ats_status(run_id, ats, "failed", finished=True,
                            error=f"timeout after {settings.local_scraper_timeout_seconds}s")
            any_failed = True
            continue
        except Exception as e:
            log.exception("[run=%d %s] shim crashed", run_id, ats)
            _set_ats_status(run_id, ats, "failed", finished=True,
                            error=f"{type(e).__name__}: {e}")
            any_failed = True
            continue

        # Apply the empty-ATS safeguard (Gate 5) and load if safe.
        try:
            progress.phase = "loading"
            progress.phase_started_at = datetime.now(timezone.utc).isoformat()
            _set_ats_status(run_id, ats, "running", progress=progress)
            load_result = local_scraper.safe_load(progress)
        except Exception as e:
            log.exception("[run=%d %s] load failed", run_id, ats)
            _set_ats_status(run_id, ats, "failed", finished=True,
                            error=f"load_failed: {type(e).__name__}: {e}",
                            progress=progress)
            any_failed = True
            continue

        if load_result.safeguard_tripped:
            progress.phase = "safeguard_skipped"
            _set_ats_status(run_id, ats, "skipped", finished=True,
                            progress=progress, load_result=load_result,
                            error="empty_scrape_safeguard_preserved_existing_rows")
            any_safeguard = True
        else:
            progress.phase = "succeeded"
            progress.rows_written = load_result.rows_written
            progress.rows_inserted = load_result.rows_inserted
            progress.rows_updated = load_result.rows_updated
            _set_ats_status(run_id, ats, "succeeded", finished=True,
                            progress=progress, load_result=load_result)
            any_succeeded = True
            # Snapshot the URLs this run loaded into a side table so the
            # per-run drill-down survives later writes (cron, other runs)
            # to the same rows. See plan v3 / scrape_run_url.
            if progress.parquet_path and progress.parquet_path.exists():
                _snapshot_run_urls(run_id, ats, progress.parquet_path)

        await handle.broadcast({
            "event": "ats_finished", "run_id": run_id, "ats": ats,
            "rows_written": load_result.rows_written,
            "rows_inserted": load_result.rows_inserted,
            "rows_updated": load_result.rows_updated,
            "safeguard_tripped": load_result.safeguard_tripped,
        })

    # Resolve overall run status
    totals = _aggregate_totals(run_id)
    if handle.cancelled():
        final = "cancelled"
    elif any_succeeded and not any_failed:
        final = "succeeded"
    elif any_succeeded:
        final = "partial"
    elif any_safeguard and not any_failed:
        final = "succeeded"  # safeguard alone isn't a failure
    else:
        final = "failed"

    _set_run_status(run_id, final, finished=True, totals=totals)
    await handle.broadcast({
        "event": "run_finished", "run_id": run_id,
        "status": final, **totals,
    })
    log.info("run %d finished: %s (%s)", run_id, final, totals)


async def start_run(ats_list: list[str], *,
                    triggered_by: str = "manual_api",
                    max_companies_per_ats: int | None = None,
                    incremental_enabled: bool = False,
                    incremental_days: int | None = None,
                    preset_id: int | None = None) -> int:
    """Claim a run slot, kick off the background orchestrator, return run_id.
    Raises SingleFlightError if another run is active.
    """
    if not ats_list:
        raise ValueError("ats_list must be non-empty")
    if len(ats_list) > settings.local_scraper_max_ats_per_run:
        raise ValueError(
            f"too many ATS in one run: got {len(ats_list)}, "
            f"max is {settings.local_scraper_max_ats_per_run}"
        )
    available = set(local_scraper.available_ats())
    bad_avail = [a for a in ats_list if a not in available]
    if bad_avail:
        raise ValueError(f"ATS not available (no vendored companies CSV): {bad_avail}")
    # Empty allow-list means "no restriction beyond availability".
    allowed = set(settings.local_scraper_allowed_ats)
    if allowed:
        bad_allow = [a for a in ats_list if a not in allowed]
        if bad_allow:
            raise ValueError(f"ATS not in allow-list: {bad_allow}")

    run_id = _claim_run(
        ats_list,
        triggered_by,
        max_companies_per_ats,
        incremental_enabled=incremental_enabled,
        incremental_days=incremental_days,
        preset_id=preset_id,
    )
    handle = RunHandle(
        run_id=run_id,
        incremental_enabled=incremental_enabled,
        incremental_days=incremental_days,
    )
    async with _runs_lock:
        _runs[run_id] = handle
    handle.task = asyncio.create_task(
        _orchestrate(run_id, handle, ats_list, max_companies_per_ats)
    )

    def _on_done(_t: asyncio.Task) -> None:
        # Remove from in-memory registry once finished; DB has the record.
        async def _drop() -> None:
            async with _runs_lock:
                _runs.pop(run_id, None)
        asyncio.create_task(_drop())

    handle.task.add_done_callback(_on_done)
    return run_id


async def cancel_run(run_id: int) -> bool:
    """Signal cancellation. Returns True if the run was active in memory."""
    async with _runs_lock:
        handle = _runs.get(run_id)
    if handle is None:
        return False
    handle.cancel_event.set()
    return True


# ---- read-side helpers (used by the router) ------------------------------


def get_run(run_id: int) -> dict | None:
    with get_db() as conn:
        run = conn.execute(
            "SELECT * FROM scrape_run WHERE id=?", (run_id,)
        ).fetchone()
        if run is None:
            return None
        ats_rows = conn.execute(
            "SELECT * FROM scrape_run_ats WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    out = dict(run)
    out["ats_requested"] = json.loads(out["ats_requested"])
    out["incremental_enabled"] = bool(out.get("incremental_enabled"))
    out["per_ats"] = [dict(r) for r in ats_rows]
    out["live"] = run_id in _runs
    return out


def list_runs(limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, started_at, finished_at, status, ats_requested, "
            "triggered_by, total_scraped, total_failed, total_written, "
            "max_companies_per_ats, total_inserted, total_updated, "
            "incremental_enabled, incremental_days, preset_id "
            "FROM scrape_run ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ats_requested"] = json.loads(d["ats_requested"])
        d["incremental_enabled"] = bool(d.get("incremental_enabled"))
        d["live"] = d["id"] in _runs
        out.append(d)
    return out


def get_run_handle(run_id: int) -> RunHandle | None:
    return _runs.get(run_id)


def get_log_path(run_id: int, ats: str) -> Path | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT log_path FROM scrape_run_ats WHERE run_id=? AND ats=?",
            (run_id, ats),
        ).fetchone()
    if not row or not row["log_path"]:
        return None
    p = Path(row["log_path"])
    return p if p.exists() else None


def list_presets() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM scrape_preset ORDER BY is_default DESC, name ASC"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["ats_requested"] = json.loads(d["ats_requested"])
        d["incremental_enabled"] = bool(d.get("incremental_enabled"))
        d["is_default"] = bool(d.get("is_default"))
        out.append(d)
    return out


def create_preset(
    *,
    name: str,
    ats_requested: list[str],
    max_companies_per_ats: int | None,
    incremental_enabled: bool,
    incremental_days: int | None,
    notes: str | None = None,
    is_default: bool = False,
) -> dict:
    with get_db() as conn:
        if is_default:
            conn.execute("UPDATE scrape_preset SET is_default=0")
        cur = conn.execute(
            "INSERT INTO scrape_preset(name, ats_requested, max_companies_per_ats, "
            "incremental_enabled, incremental_days, notes, is_default) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name.strip(), json.dumps(ats_requested), max_companies_per_ats,
                1 if incremental_enabled else 0, incremental_days,
                notes, 1 if is_default else 0,
            ),
        )
        pid = int(cur.lastrowid)
    return get_preset(pid)


def get_preset(preset_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM scrape_preset WHERE id=?", (preset_id,)).fetchone()
    if row is None:
        raise ValueError(f"preset {preset_id} not found")
    d = dict(row)
    d["ats_requested"] = json.loads(d["ats_requested"])
    d["incremental_enabled"] = bool(d.get("incremental_enabled"))
    d["is_default"] = bool(d.get("is_default"))
    return d


def update_preset(preset_id: int, **fields: Any) -> dict:
    current = get_preset(preset_id)
    merged = {**current, **fields}
    with get_db() as conn:
        if merged.get("is_default"):
            conn.execute("UPDATE scrape_preset SET is_default=0")
        conn.execute(
            "UPDATE scrape_preset SET name=?, ats_requested=?, max_companies_per_ats=?, "
            "incremental_enabled=?, incremental_days=?, notes=?, is_default=?, "
            "updated_at=datetime('now') WHERE id=?",
            (
                str(merged["name"]).strip(),
                json.dumps(merged["ats_requested"]),
                merged.get("max_companies_per_ats"),
                1 if merged.get("incremental_enabled") else 0,
                merged.get("incremental_days"),
                merged.get("notes"),
                1 if merged.get("is_default") else 0,
                preset_id,
            ),
        )
    return get_preset(preset_id)


def delete_preset(preset_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM scrape_preset WHERE id=?", (preset_id,))


def coverage_summary(ats_list: list[str] | None = None) -> dict[str, dict[str, int]]:
    targets = ats_list or local_scraper.available_ats()
    out: dict[str, dict[str, int]] = {}
    now = datetime.now(timezone.utc)
    for ats in targets:
        companies = local_scraper.read_company_catalog(ats)
        by_slug = {c.slug for c in companies}
        buckets = {"never": 0, "0_1d": 0, "2_7d": 0, "8_30d": 0, "gt_30d": 0}
        if not by_slug:
            out[ats] = buckets
            continue
        with get_db() as conn:
            rows = conn.execute(
                "SELECT slug, last_scraped_at FROM scrape_company_state WHERE ats=?",
                (ats,),
            ).fetchall()
        rec = {str(r["slug"]): r["last_scraped_at"] for r in rows}
        for slug in by_slug:
            ts = rec.get(slug)
            if not ts:
                buckets["never"] += 1
                continue
            try:
                parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                delta = now - parsed
                days = delta.total_seconds() / 86400.0
            except Exception:
                buckets["gt_30d"] += 1
                continue
            if days <= 1:
                buckets["0_1d"] += 1
            elif days <= 7:
                buckets["2_7d"] += 1
            elif days <= 30:
                buckets["8_30d"] += 1
            else:
                buckets["gt_30d"] += 1
        out[ats] = buckets
    return out


def coverage_detail(ats: str, limit: int = 500) -> list[dict]:
    companies = local_scraper.read_company_catalog(ats)
    if not companies:
        return []
    with get_db() as conn:
        rows = conn.execute(
            "SELECT slug, last_scraped_at, last_status, success_count, failure_count "
            "FROM scrape_company_state WHERE ats=?",
            (ats,),
        ).fetchall()
    state = {str(r["slug"]): dict(r) for r in rows}
    out: list[dict] = []
    for c in companies:
        s = state.get(c.slug, {})
        out.append({
            "name": c.name,
            "slug": c.slug,
            "url": c.url,
            "last_scraped_at": s.get("last_scraped_at"),
            "last_status": s.get("last_status"),
            "success_count": int(s.get("success_count") or 0),
            "failure_count": int(s.get("failure_count") or 0),
        })
    out.sort(key=lambda x: (x["last_scraped_at"] is None, x["last_scraped_at"] or ""))
    return out[:limit]
