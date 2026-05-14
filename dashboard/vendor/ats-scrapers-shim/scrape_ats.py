"""Per-ATS scraper runner — invoked as a subprocess by the dashboard.

Run via:
  uv run --directory <dashboard>/vendor/ats-scrapers --with pyarrow \\
      python <dashboard>/vendor/ats-scrapers-shim/scrape_ats.py \\
      <ats> --output <parquet> --companies-csv <csv> \\
      [--max-companies N] [--concurrency N]

Reads ats-companies/<ats>.csv, iterates company slugs, calls the matching
jobhive scraper for each, collects all Job rows into one parquet at --output.

Concurrency: companies within an ATS are scraped in parallel, bounded by
--concurrency (default 8). jobhive's per-scraper fetch() is synchronous,
so each company runs in a worker thread via asyncio.to_thread — the main
asyncio loop just coordinates the semaphore and emits NDJSON events. Cancel
still works the same way (parent SIGTERMs the process group → all threads
die with the process).

Emits NDJSON progress events on stderr — one event per line — so the parent
process can stream live counters into scrape_run_ats. With parallel
companies, events for different companies interleave; counters are still
correct because the parent tallies by event type, not by order.

This shim lives OUTSIDE vendor/ats-scrapers so the vendored tree stays a
clean upstream snapshot that can be bumped without merge conflicts.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
import traceback
from pathlib import Path

from jobhive.scrapers import get_scraper

import pandas as pd


def emit(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}) + "\n")
    sys.stderr.flush()


def read_companies(csv_path: Path) -> list[tuple[str, str, str]]:
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    return [(r["name"], r["slug"], r.get("url", "")) for r in rows]


def job_to_row(job: object, ats: str) -> dict:
    d = job.model_dump(mode="json")  # type: ignore[attr-defined]
    d.setdefault("ats_type", ats)
    # ``raw`` holds provider-specific overflow with heterogeneous inner
    # shapes (Greenhouse ``metadata[].value`` is sometimes a string,
    # sometimes a list). PyArrow's struct inference rejects that mix
    # ("cannot mix list and non-list, non-null values") when pandas hands
    # it the column. Serialize to JSON to match jobhive's CSV contract.
    if d.get("raw") is not None and not isinstance(d["raw"], str):
        d["raw"] = json.dumps(d["raw"], default=str)
    return d


def _scrape_one_sync(ats: str, slug: str) -> list[object]:
    """Synchronous body that runs in a worker thread.

    jobhive's scrapers use asyncio.run() internally inside fetch(), which
    cannot run in an already-running event loop. Putting fetch() in its own
    thread sidesteps that — each worker thread is free to spin up its own
    short-lived event loop.
    """
    return list(get_scraper(ats, slug).fetch())


async def _scrape_one(
    sem: asyncio.Semaphore,
    ats: str,
    idx: int,
    name: str,
    slug: str,
) -> tuple[list[dict], bool]:
    """One company, bounded by the semaphore. Emits progress events and
    returns (rows, ok). `ok=False` means the scraper raised; `ok=True`
    with an empty row list means the board is legitimately empty."""
    async with sem:
        emit("company_started", ats=ats, slug=slug, name=name, index=idx)
        t0 = time.time()
        try:
            jobs = await asyncio.to_thread(_scrape_one_sync, ats, slug)
        except Exception as e:
            emit(
                "company_failed", ats=ats, slug=slug, index=idx,
                error=type(e).__name__, message=str(e)[:300],
                elapsed=round(time.time() - t0, 2),
            )
            return [], False
        rows = [job_to_row(j, ats) for j in jobs]
        emit(
            "company_succeeded", ats=ats, slug=slug, index=idx,
            rows=len(jobs), elapsed=round(time.time() - t0, 2),
        )
        return rows, True


async def _run(args: argparse.Namespace) -> int:
    try:
        companies = read_companies(args.companies_csv)
    except FileNotFoundError:
        emit("run_failed", ats=args.ats, error="companies_csv_missing",
             path=str(args.companies_csv))
        return 2

    if args.slugs_file:
        wanted = [s.strip() for s in args.slugs_file.read_text().splitlines() if s.strip()]
        wanted_set = set(wanted)
        ordered = [row for row in companies if row[1] in wanted_set]
        by_slug = {slug: row for row in ordered for slug in [row[1]]}
        companies = [by_slug[s] for s in wanted if s in by_slug]
    elif args.max_companies is not None:
        companies = companies[: args.max_companies]

    concurrency = max(1, args.concurrency)
    emit("run_started", ats=args.ats,
         companies_total=len(companies),
         concurrency=concurrency)

    sem = asyncio.Semaphore(concurrency)
    tasks = [
        _scrape_one(sem, args.ats, idx, name, slug)
        for idx, (name, slug, _url) in enumerate(companies)
    ]
    results: list[tuple[list[dict], bool]] = await asyncio.gather(*tasks)

    all_rows: list[dict] = [r for rows, _ok in results for r in rows]
    succeeded = sum(1 for _rows, ok in results if ok)
    failed = sum(1 for _rows, ok in results if not ok)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if all_rows:
        try:
            pd.DataFrame(all_rows).to_parquet(args.output, engine="pyarrow", index=False)
            emit("parquet_written", path=str(args.output), rows=len(all_rows))
        except Exception as e:
            emit("parquet_write_failed", error=type(e).__name__, message=str(e)[:300])
            traceback.print_exc(file=sys.stderr)
            return 3
    else:
        emit("parquet_skipped_empty", path=str(args.output))

    emit(
        "run_completed", ats=args.ats,
        companies_total=len(companies),
        companies_succeeded=succeeded,
        companies_failed=failed,
        rows_total=len(all_rows),
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ats")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--companies-csv", required=True, type=Path)
    ap.add_argument("--max-companies", type=int, default=None)
    ap.add_argument("--slugs-file", type=Path, default=None)
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Max concurrent company scrapes (default 8)")
    args = ap.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
