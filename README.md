# applyd

A microservices monorepo for searching, filtering, and (eventually) auto-applying to jobs across 40+ ATS platforms.

Powered by the open-source [jobhive](https://data.stapply.ai/) dataset — ~3.8M live job postings refreshed every 24 hours.

## Services

| Service | Path | Status | Description |
|---|---|---|---|
| **MS1 — Dashboard** | `dashboard/` | In development | FastAPI + Jinja2 web app for browsing, searching, and saving jobs |
| **MS2 — Auto-apply agent** | `agent/` | Planned | Consumes saved jobs from MS1 and submits applications automatically |

## MS1 (dashboard) — quick start

```bash
cd dashboard
uv sync                           # install dependencies into .venv
cp .env.example .env              # configure local settings
uv run python -m app.cli ingest   # first ingestion (~3-8 min)
uv run uvicorn app.main:app --reload
```

Then visit `http://localhost:8000`.

## How it works

1. **Daily ingestion**: APScheduler pulls per-ATS Parquet files from `storage.stapply.ai`, streams them into a local SQLite DB. sha256 cache short-circuits unchanged files.
2. **30-day rolling window**: Each row carries `first_seen_at` (set on first INSERT, preserved by upserts). The **effective date** is `COALESCE(posted_at, first_seen_at)` — used everywhere. Anything older than 30 days is pruned. Storage matches the UI's max time filter.
3. **Why the synthetic date**: ~34% of upstream rows (Workday, SuccessFactors, FAANG via custom APIs) have NULL `posted_at`. Rather than drop them, we use the date we first observed each URL as a fallback timestamp. Bootstraps over ~30 days into a natural distribution.
4. **Country tagging**: Each row's raw `location` string is regex-matched (USA cities + state codes with comma context + state names) to flag US jobs at ingest time.
5. **Search**: SQLite FTS5 indexes `title + company + description + location` for fast full-text search; rebuild after bulk upsert.
6. **Default view**: USA jobs in the last 24 hours by effective date; the unified dashboard exposes filters for ATS, salary, employment type, country, remote, and time windows up to 30 days.

## License

MIT (this repo). Built on top of [ats-scrapers](https://github.com/kalil0321/ats-scrapers), which is also MIT-licensed. The upstream jobhive dataset is MIT-licensed.
