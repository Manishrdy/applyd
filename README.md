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

## Quick Start (MS1 Dashboard)

```bash
cd dashboard
uv sync
cp .env.example .env
uv run python -m app.cli ingest
uv run uvicorn app.main:app --reload
```

Open: [http://localhost:8000](http://localhost:8000)

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

## License

MIT (this repository).  
Built on top of [ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT).  
Upstream jobhive dataset and pipeline are MIT-licensed.
