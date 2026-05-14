"""LocalScraperSource — invokes the vendored jobhive scrapers in a subprocess
per ATS, streams NDJSON progress events, then feeds the resulting parquet
through the existing process_parquet upsert path.

The daily jobhive cron (manifest path) is intentionally unaffected by this
module. Manual scrapes never prune (Gate 5) — they only upsert. Dedup is
inherited from jobs.url UNIQUE + ON CONFLICT(url) DO UPDATE.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import signal
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import pandas as pd

from app.config import settings
from app.database import get_db
from app.services.ingestion import process_parquet

log = logging.getLogger(__name__)

# vendor/ tree, sibling to app/
_DASHBOARD_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = _DASHBOARD_ROOT / "vendor" / "ats-scrapers"
SHIM_SCRIPT = _DASHBOARD_ROOT / "vendor" / "ats-scrapers-shim" / "scrape_ats.py"
VENDORED_FROM_FILE = _DASHBOARD_ROOT / "vendor" / "VENDORED_FROM"


@dataclass
class ScrapeProgress:
    """Live counters for one (run, ATS) pair. Mutated in place as events arrive."""
    ats: str
    companies_total: int = 0
    companies_succeeded: int = 0
    companies_failed: int = 0
    rows_scraped: int = 0          # produced by scraper (across all succeeded companies)
    rows_written: int = 0          # upserted into jobs table (set after process_parquet)
    rows_inserted: int = 0
    rows_updated: int = 0
    parquet_path: Path | None = None
    selected_companies: int = 0
    phase: str = "pending"
    phase_started_at: str | None = None
    eta_seconds: int | None = None
    throughput_cpm: float | None = None
    last_event: dict | None = field(default=None, repr=False)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)


ProgressCallback = Callable[[ScrapeProgress], Awaitable[None]]


def vendor_commit_sha() -> str | None:
    """Parse the commit SHA out of vendor/VENDORED_FROM."""
    if not VENDORED_FROM_FILE.exists():
        return None
    for line in VENDORED_FROM_FILE.read_text().splitlines():
        if line.startswith("commit:"):
            return line.split(":", 1)[1].strip()
    return None


def available_ats() -> list[str]:
    """ATS names with a companies CSV in the vendored tree."""
    ats_dir = VENDOR_DIR / "ats-companies"
    if not ats_dir.exists():
        return []
    return sorted(p.stem for p in ats_dir.glob("*.csv") if p.stem != "README")


async def _noop_progress(_: ScrapeProgress) -> None:
    return None


class ScrapeCancelled(Exception):
    """Raised by run_one_ats when cancel_event is set during a run."""


@dataclass(frozen=True)
class CompanyRef:
    name: str
    slug: str
    url: str


def read_company_catalog(ats: str) -> list[CompanyRef]:
    companies_csv = VENDOR_DIR / "ats-companies" / f"{ats}.csv"
    if not companies_csv.exists():
        return []
    with companies_csv.open() as f:
        rows = list(csv.DictReader(f))
    return [
        CompanyRef(name=str(r.get("name") or ""), slug=str(r.get("slug") or ""), url=str(r.get("url") or ""))
        for r in rows
        if str(r.get("slug") or "").strip()
    ]


def _cursor_for_ats(conn, ats: str) -> int:
    row = conn.execute("SELECT next_index FROM scrape_ats_cursor WHERE ats=?", (ats,)).fetchone()
    return int(row["next_index"]) if row else 0


def _set_cursor_for_ats(conn, ats: str, next_index: int) -> None:
    conn.execute(
        "INSERT INTO scrape_ats_cursor(ats, next_index, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(ats) DO UPDATE SET next_index=excluded.next_index, updated_at=datetime('now')",
        (ats, max(0, int(next_index))),
    )


def select_companies_for_run(
    ats: str,
    *,
    max_companies: int | None,
    incremental_enabled: bool = False,
    incremental_days: int | None = None,
) -> list[CompanyRef]:
    companies = read_company_catalog(ats)
    if not companies:
        return []
    total = len(companies)
    if max_companies is None:
        max_companies = total
    take = min(max_companies, total)
    now = datetime.now(timezone.utc)
    cutoff_iso = None
    if incremental_enabled and incremental_days and incremental_days > 0:
        cutoff_iso = (now - pd.Timedelta(days=incremental_days)).isoformat()

    with get_db() as conn:
        cursor = _cursor_for_ats(conn, ats) % total
        rr = [companies[(cursor + i) % total] for i in range(total)]
        selected: list[CompanyRef] = []
        selected_slugs: set[str] = set()
        if cutoff_iso:
            marks = conn.execute(
                "SELECT slug, last_scraped_at FROM scrape_company_state WHERE ats=?",
                (ats,),
            ).fetchall()
            recent = {
                str(m["slug"]) for m in marks
                if m["last_scraped_at"] and str(m["last_scraped_at"]) >= cutoff_iso
            }
            for c in rr:
                if c.slug not in recent:
                    selected.append(c)
                    selected_slugs.add(c.slug)
                    if len(selected) >= take:
                        break
        if len(selected) < take:
            for c in rr:
                if c.slug in selected_slugs:
                    continue
                selected.append(c)
                selected_slugs.add(c.slug)
                if len(selected) >= take:
                    break
        _set_cursor_for_ats(conn, ats, (cursor + len(selected)) % total)
    return selected


def mark_company_attempt(
    ats: str,
    slug: str,
    *,
    name: str | None,
    source_url: str | None,
    status: str,
    rows: int,
    run_id: int | None,
) -> None:
    if not slug:
        return
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scrape_company_state(ats, slug, name, source_url, last_scraped_at, "
            "last_run_id, last_status, success_count, failure_count, total_rows_scraped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ats, slug) DO UPDATE SET "
            "name=COALESCE(excluded.name, scrape_company_state.name), "
            "source_url=COALESCE(excluded.source_url, scrape_company_state.source_url), "
            "last_scraped_at=excluded.last_scraped_at, "
            "last_run_id=excluded.last_run_id, "
            "last_status=excluded.last_status, "
            "success_count=scrape_company_state.success_count + excluded.success_count, "
            "failure_count=scrape_company_state.failure_count + excluded.failure_count, "
            "total_rows_scraped=scrape_company_state.total_rows_scraped + excluded.total_rows_scraped",
            (
                ats, slug, name, source_url, datetime.now(timezone.utc).isoformat(),
                run_id, status,
                1 if status == "succeeded" else 0,
                1 if status == "failed" else 0,
                max(0, int(rows)),
            ),
        )


async def run_one_ats(
    ats: str,
    output_parquet: Path,
    log_path: Path,
    *,
    max_companies: int | None,
    timeout_seconds: int,
    on_progress: ProgressCallback = _noop_progress,
    cancel_event: asyncio.Event | None = None,
    per_company_concurrency: int = 8,
    selected_slugs: list[str] | None = None,
    run_id: int | None = None,
) -> ScrapeProgress:
    """Run the shim for one ATS. Streams NDJSON events from stderr through
    `on_progress`. Does NOT load the parquet into the DB — caller does that
    via `load_parquet_into_db()` after deciding whether the empty-ATS
    safeguard (Gate 5) should block the load.

    Raises asyncio.TimeoutError on hard timeout, ScrapeCancelled if
    `cancel_event` is set mid-run. In both cases the process group is killed
    before the exception propagates.
    """
    companies_csv = VENDOR_DIR / "ats-companies" / f"{ats}.csv"
    if not companies_csv.exists():
        raise FileNotFoundError(f"vendor/ats-scrapers/ats-companies/{ats}.csv not found")

    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "uv", "run",
        "--directory", str(VENDOR_DIR),
        "--with", "pyarrow",
        "python", str(SHIM_SCRIPT),
        ats,
        "--output", str(output_parquet),
        "--companies-csv", str(companies_csv),
        "--concurrency", str(max(1, per_company_concurrency)),
    ]
    slug_file_path: Path | None = None
    if selected_slugs:
        fd, tmp = tempfile.mkstemp(prefix=f"scrape-{ats}-", suffix=".slugs")
        os.close(fd)
        slug_file_path = Path(tmp)
        slug_file_path.write_text("\n".join(selected_slugs))
        cmd += ["--slugs-file", str(slug_file_path)]
    elif max_companies is not None:
        cmd += ["--max-companies", str(max_companies)]

    log.info("[%s] launching shim (max_companies=%s, timeout=%ds)",
             ats, max_companies, timeout_seconds)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # own process group → can SIGTERM the whole tree
    )

    progress = ScrapeProgress(ats=ats)
    progress.selected_companies = len(selected_slugs or [])
    progress.phase = "scraping"
    progress.phase_started_at = datetime.now(timezone.utc).isoformat()

    async def consume_stderr() -> None:
        assert proc.stderr is not None
        with log_path.open("w") as logf:
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                text = raw.decode("utf-8", errors="replace")
                logf.write(text)
                stripped = text.strip()
                if not stripped or not stripped.startswith("{"):
                    continue
                try:
                    evt = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                progress.last_event = evt
                kind = evt.get("event")
                if kind == "run_started":
                    progress.companies_total = int(evt.get("companies_total", 0))
                    if progress.selected_companies == 0:
                        progress.selected_companies = progress.companies_total
                elif kind == "company_succeeded":
                    progress.companies_succeeded += 1
                    rows = int(evt.get("rows", 0))
                    progress.rows_scraped += rows
                    mark_company_attempt(
                        ats,
                        str(evt.get("slug") or ""),
                        name=str(evt.get("name") or "") or None,
                        source_url=None,
                        status="succeeded",
                        rows=rows,
                        run_id=run_id,
                    )
                elif kind == "company_failed":
                    progress.companies_failed += 1
                    mark_company_attempt(
                        ats,
                        str(evt.get("slug") or ""),
                        name=str(evt.get("name") or "") or None,
                        source_url=None,
                        status="failed",
                        rows=0,
                        run_id=run_id,
                    )
                elif kind == "parquet_written":
                    progress.parquet_path = Path(evt["path"])
                elif kind == "parquet_skipped_empty":
                    progress.parquet_path = None
                done = progress.companies_succeeded + progress.companies_failed
                elapsed_s = max(1.0, time.monotonic() - progress.started_monotonic)
                progress.throughput_cpm = (done / elapsed_s) * 60.0
                left = max(0, (progress.companies_total or progress.selected_companies) - done)
                progress.eta_seconds = int((left / progress.throughput_cpm) * 60) if progress.throughput_cpm and left > 0 else 0
                await on_progress(progress)

    async def drain_stdout() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return

    cancel_watcher: asyncio.Task | None = None
    if cancel_event is not None:
        async def _watch_cancel() -> None:
            await cancel_event.wait()
            log.info("[%s] cancel signal received — terminating shim", ats)
            await _kill_group(proc)
        cancel_watcher = asyncio.create_task(_watch_cancel())

    try:
        try:
            await asyncio.wait_for(
                asyncio.gather(consume_stderr(), drain_stdout(), proc.wait()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("[%s] shim hit %ds timeout — killing process group", ats, timeout_seconds)
            await _kill_group(proc)
            raise
    finally:
        if slug_file_path and slug_file_path.exists():
            slug_file_path.unlink(missing_ok=True)
        if cancel_watcher is not None and not cancel_watcher.done():
            cancel_watcher.cancel()
            try:
                await cancel_watcher
            except (asyncio.CancelledError, Exception):
                pass

    if cancel_event is not None and cancel_event.is_set():
        raise ScrapeCancelled(f"[{ats}] cancelled mid-run")

    if proc.returncode != 0:
        log.warning("[%s] shim exited with code %s (treated as partial)", ats, proc.returncode)

    log.info("[%s] shim done: companies %d/%d succeeded, %d rows scraped",
             ats, progress.companies_succeeded, progress.companies_total,
             progress.rows_scraped)
    return progress


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


@dataclass
class LoadResult:
    """Outcome of attempting to load one ATS's parquet into the DB."""
    ats: str
    rows_seen: int = 0
    rows_written: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    safeguard_tripped: bool = False
    rows_skipped_safeguard: int = 0  # existing DB rows preserved by the safeguard
    skipped_reason: str | None = None  # 'empty_scrape_with_existing' | 'empty_scrape_no_existing' | None


def existing_row_count(ats: str) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE ats_type = ?", (ats,)
        ).fetchone()
    return int(row[0]) if row else 0


def load_parquet_into_db(parquet_path: Path, ats: str) -> tuple[int, int, int, int]:
    """Upsert a scraper-produced parquet using the same path the manifest cron uses.

    Returns (rows_seen, rows_upserted). NO prune is run — manual scrape is
    additive only. Dedup is guaranteed by jobs.url UNIQUE + ON CONFLICT
    DO UPDATE (see services/ingestion.UPSERT_SQL).
    """
    fetched_cycle = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff_ts = pd.Timestamp(
        datetime.now(timezone.utc) - pd.Timedelta(days=settings.rolling_window_days)
    )
    with get_db() as conn:
        return process_parquet(
            parquet_path, ats, conn, cutoff_ts, fetched_cycle,
            settings.ingest_batch_size,
        )


def safe_load(progress: ScrapeProgress) -> LoadResult:
    """Apply the empty-ATS safeguard, then load if safe.

    Rule (signed off in plan v2, decision 5): if the scraper produced 0 rows
    for an ATS that already has rows in the DB, do NOT load the (empty)
    parquet. Existing rows are preserved untouched; the daily manifest cron
    will refresh them the next time it runs.

    The four cases:
      A. scraper produced rows + parquet present       → load normally
      B. scraper produced 0 rows + DB has existing rows → safeguard trips, skip load
      C. scraper produced 0 rows + DB is empty for ats  → no-op, no safeguard needed
      D. scraper produced rows but parquet write failed → caller treats as failure

    Cases A/B/C are handled here. Case D is detectable by `progress.parquet_path
    is None` while `progress.rows_scraped > 0` and is reported as a load
    failure to the caller.
    """
    ats = progress.ats
    result = LoadResult(ats=ats)

    if progress.rows_scraped == 0:
        existing = existing_row_count(ats)
        if existing > 0:
            log.warning(
                "[%s] empty-ATS safeguard: scraper returned 0 rows, %d rows exist "
                "in DB — skipping load to preserve existing data",
                ats, existing,
            )
            result.safeguard_tripped = True
            result.rows_skipped_safeguard = existing
            result.skipped_reason = "empty_scrape_with_existing"
            return result
        # Case C: no data, nothing to preserve.
        result.skipped_reason = "empty_scrape_no_existing"
        return result

    # progress.rows_scraped > 0 — we expect a parquet to exist.
    if progress.parquet_path is None or not progress.parquet_path.exists():
        raise FileNotFoundError(
            f"[{ats}] scraper reported {progress.rows_scraped} rows but no "
            f"parquet at {progress.parquet_path!r}"
        )

    seen, written, inserted, updated = load_parquet_into_db(progress.parquet_path, ats)
    result.rows_seen = seen
    result.rows_written = written
    result.rows_inserted = inserted
    result.rows_updated = updated
    return result
