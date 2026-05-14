# applyd

**The operating system for modern job search.**  
`applyd` helps teams and power users discover, track, and prioritize high-signal opportunities across dozens of ATS ecosystems from a single interface.

Built on top of the open-source [jobhive](https://data.stapply.ai/) pipeline and extended with a local scraping control plane, `applyd` gives you both **fresh global coverage** and **manual override controls** when you need precision.

---

## Why applyd

Job discovery is fragmented. Every ATS has different structures, quality, and update behavior. `applyd` unifies this into one searchable system with:

- Daily ingestion of multi-ATS job feeds
- Fast local filtering and full-text search
- Saved-job workflow for downstream automation
- Manual scrape controls when upstream freshness is not enough

---

## Product Overview

### MS1: Dashboard (active)
A FastAPI + Jinja2 application for:

- Searching jobs across ATS providers
- Filtering by recency, country, salary, employment type, remote, etc.
- Saving jobs for later action
- Running local scraper jobs with live progress, logs, and run history

### MS2: Auto-Apply Agent (planned)
A future service that will consume curated/saved jobs and automate application workflows.

---

## Core Capabilities

### 1) Unified job index
- Ingests ATS parquet sources into local SQLite
- Deduplicates by canonical job URL (`ON CONFLICT(url) DO UPDATE`)
- Preserves discoverability for undated jobs via `first_seen_at`

### 2) Time-aware freshness model
- Effective date model: `COALESCE(posted_at, first_seen_at)`
- 30-day rolling retention window
- Storage and UI filter windows stay aligned

### 3) Search + analytics-grade filtering
- SQLite FTS5 on title/company/description/location
- Country and remote filtering
- Salary normalization and bucketing
- ATS and company-level slices

### 4) Local scraper operations console
- Trigger manual scrapes per ATS
- Limit companies per ATS
- Stream live progress with SSE
- View per-ATS logs and run outcomes
- Preserve upsert safety (manual runs are additive; no prune)

---

## Architecture at a glance

```text
Upstream jobhive parquet sources
          |
          v
Daily ingestion scheduler (APScheduler)
          |
          v
SQLite (jobs, saved_jobs, logs, scrape runs)
          |
          +--> Dashboard APIs (FastAPI)
          |
          +--> UI (Jinja2 + AlpineJS)
          |
          +--> Local scraper subprocess orchestration
```

### Important data paths
- **Daily manifest path**: scheduled ingestion from upstream
- **Manual local-scraper path**: operator-triggered runs for targeted ATS refreshes

Both converge into the same `jobs` table through the same upsert contract.

---

## Repository Structure

```text
applyd/
├─ dashboard/                 # MS1 web app (FastAPI + Jinja2)
│  ├─ app/                    # APIs, services, scheduler, DB layer
│  ├─ static/                 # frontend assets
│  ├─ templates/              # UI templates
│  └─ vendor/                 # vendored scraper dependencies + shim
├─ agent/                     # MS2 (planned)
└─ README.md
```

---

## Setup Instructions (Detailed)

### 1) Clone repository

```bash
git clone https://github.com/Manishrdy/applyd.git
cd applyd
```

### 2) Prerequisites

Install these on your machine:

- Python `3.12+`
- `uv` ([install guide](https://docs.astral.sh/uv/getting-started/installation/))
- Docker (for local Redis)

### 3) Install dashboard dependencies

```bash
cd dashboard
uv sync
```

### 4) Configure environment

```bash
cp .env.example .env
```

Update `.env` as needed for your machine. For local development, defaults are typically enough.

### 5) Start Redis (Docker)

```bash
docker pull redis:7-alpine
docker run -d --name applyd-redis -p 6379:6379 redis:7-alpine
docker ps --filter "name=applyd-redis"
```

Useful Redis container commands:

```bash
docker logs -f applyd-redis
docker stop applyd-redis
docker start applyd-redis
docker rm -f applyd-redis
```

### 6) Initialize local data + ingest jobs

From `dashboard/`:

```bash
uv run python -m app.cli init-db
uv run python -m app.cli sync-company-catalogs --dry-run
uv run python -m app.cli ingest
uv run python -m app.cli stats
```

What this does:

- Creates SQLite schema
- Checks ATS company catalog freshness
- Ingests upstream jobs into local DB
- Prints health/stats summary

### 7) Run the dashboard app

```bash
uv run uvicorn app.main:app --reload
```

Open: [http://localhost:8000](http://localhost:8000)

### 8) Trigger ingest manually (optional)

CLI:

```bash
uv run python -m app.cli ingest
```

API:

```bash
curl -X POST "http://localhost:8000/api/ingest?force=false"
```

Notes:

- Scheduler runs daily ingestion at `11:00 UTC`.
- Every ingestion attempt (scheduled, catch-up, or manual) also syncs ATS company catalogs in parallel.

---

## Local Scraper: Operator Workflow

Use `/scrape` in the dashboard to run targeted ATS refreshes.

Typical flow:

1. Choose one or more ATS sources
2. Set `max companies per ATS`
3. Start run and monitor live cards/logs
4. Review run detail and row write outcomes

### Company lists
Vendored ATS company lists live under:

- `dashboard/vendor/ats-scrapers/ats-companies/<ats>.csv`

Example:

- `dashboard/vendor/ats-scrapers/ats-companies/ashby.csv`

To refresh these catalogs from upstream:

```bash
cd dashboard
uv run python -m app.cli sync-company-catalogs --dry-run
uv run python -m app.cli sync-company-catalogs
```

This repository also includes a weekly GitHub Actions workflow
(`.github/workflows/sync-ats-company-catalogs.yml`) that opens a PR when
upstream ATS company CSVs change.

Additionally, every ingestion attempt (`11:00 UTC` daily run, catch-up poll,
or manual `/api/ingest` / CLI ingest) triggers ATS catalog sync in parallel.
This means catalog refresh still runs even when the manifest/job ingest cycle
is skipped due to unchanged upstream `updated_at`.

---

## Operational Notes

- Manual scrapes do **not** prune old rows
- Dedup/upsert is URL-based
- Empty-scrape safeguards protect existing ATS data from accidental wipe patterns
- Single-flight run enforcement prevents concurrent manual scrape runs

---

## Tech Stack

- **Backend**: FastAPI, SQLite, APScheduler, Pandas, PyArrow
- **Frontend**: Jinja2 templates, AlpineJS, Tailwind-style utility classes
- **Runtime tooling**: `uv` for dependency + command workflow

---

## Roadmap

- Auto-apply agent service (MS2)
- Richer run intelligence and coverage analytics
- Policy-driven targeting and prioritization
- Team workflows and shared configuration surfaces

---

## Release Process

Use this workflow when publishing a new version (including `v1.0.0`):

1. Ensure local quality checks pass:
   ```bash
   cd dashboard
   uv sync
   uv run --group dev pytest
   ```
2. Update release notes using:
   - `.github/RELEASE_TEMPLATE.md`
3. Commit changes on `main`.
4. Create and push a semantic version tag:
   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```
5. In GitHub, create a Release from that tag and paste/fill notes from the template.

For this repository, release notes should always call out:
- Dashboard behavior changes (`/`, `/saved`, `/scrape`, `/stats`, `/settings`)
- Data freshness/retention changes
- Any schema/env var/upgrade steps

---

## License

MIT (this repository).  
Built on top of [ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT).  
Upstream jobhive dataset and pipeline are MIT-licensed.
