# MS1 — Applyd: Development Plan

> **Goal**: A FastAPI-powered multi-page job dashboard that pulls from the
> applyd dataset, caches in SQLite, refreshes every 24 hours, and lets
> users interactively search/filter jobs — defaulting to all USA jobs posted
> in the last 24 hours.

---

## 1. Key Discoveries from the Manifest

Live manifest endpoint:
```
https://storage.stapply.ai/jobhive/v1/manifest.json
```

Critical findings:

- **Total jobs**: 3,878,668 across 47 ATS platforms
- **Schema version**: 2.0
- **Manifest refreshes**: Every 24 hours (`updated_at` field is the source of truth)
- **Per-ATS Parquet files**: Available for every ATS individually — this is the
  key to efficient selective downloading instead of pulling the full 264MB
  all.parquet every cycle
- **Canonical schema columns**:
  `url, title, company, ats_type, ats_id, location, is_remote, salary_min,
  salary_max, salary_currency, salary_period, salary_summary, employment_type,
  department, team, description, posted_at, requisition_id, apply_url,
  commitment, raw`
- **No `lat/lon` in schema v2.0** — location is a raw string; filtering by
  USA must be done via string matching on the `location` field
- **No `fetched_at` or `experience` in v2.0 schema** — README mentions these
  but manifest confirms they are not in the current schema

### ATS platforms relevant for USA jobs (high row counts):

| ATS | Rows | Parquet URL |
|---|---|---|
| Workday | 735,327 | `.../workday/jobs.parquet` |
| EURES | 1,498,837 | EU-focused, skip for USA |
| Bundesagentur | 680,932 | DE-focused, skip for USA |
| SmartRecruiters | 214,590 | `.../smartrecruiters/jobs.parquet` |
| SuccessFactors | 181,560 | `.../successfactors/jobs.parquet` |
| Greenhouse | 169,002 | `.../greenhouse/jobs.parquet` |
| Oracle | 137,555 | `.../oracle/jobs.parquet` |
| iCIMS | 120,727 | `.../icims/jobs.parquet` |
| Lever | 69,142 | `.../lever/jobs.parquet` |
| JazzHR | 71,018 | `.../jazzhr/jobs.parquet` |
| Ashby | 44,417 | `.../ashby/jobs.parquet` |
| Amazon | 28,438 | `.../amazon/jobs.parquet` |
| BambooHR | 21,196 | `.../bamboohr/jobs.parquet` |
| Rippling | 14,513 | `.../rippling/jobs.parquet` |
| Phenom | 56,831 | `.../phenom/jobs.parquet` |
| Teamtailor | 15,113 | `.../teamtailor/jobs.parquet` |
| Apple | 5,073 | `.../apple/jobs.parquet` |
| Google | 3,656 | `.../google/jobs.parquet` |
| TikTok | 3,544 | `.../tiktok/jobs.parquet` |
| Uber | 1,004 | `.../uber/jobs.parquet` |
| Meta | 454 | `.../meta/jobs.parquet` |
| Tesla | 6,165 | `.../tesla/jobs.parquet` |

> **Strategy**: Download per-ATS Parquet files selectively. Skip
> Bundesagentur, EURES, Arbetsformedlingen (EU/DE/SE only). This reduces
> download from ~264MB to ~150MB per cycle.

---

## 2. Project Structure

```
ms1_dashboard/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # Settings (paths, manifest URL, ATS list)
│   ├── database.py              # SQLite connection + table setup
│   ├── scheduler.py             # APScheduler 24hr cron job
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── dashboard.py         # HTML page routes (Jinja2)
│   │   ├── jobs.py              # JSON API endpoints
│   │   └── stats.py             # Stats/analytics endpoints
│   ├── services/
│   │   ├── __init__.py
│   │   ├── ingestion.py         # Download + parse parquet, load to SQLite
│   │   ├── query.py             # Pandas/SQLite query logic
│   │   └── manifest.py          # Manifest fetch + diff logic
│   └── templates/
│       ├── base.html            # Base layout with Bootstrap
│       ├── index.html           # Main dashboard (24hr USA jobs)
│       ├── search.html          # Search/filter page
│       ├── job_detail.html      # Single job detail page
│       └── stats.html           # Analytics/stats page
├── static/
│   ├── css/
│   │   └── app.css
│   └── js/
│       ├── dashboard.js         # Lazy loading, infinite scroll
│       ├── search.js            # Search form + filters
│       └── charts.js            # Stats charts (Chart.js)
├── data/
│   └── applyd.db               # SQLite database (gitignored)
├── cache/
│   └── parquet/                 # Downloaded parquet files (gitignored)
├── requirements.txt
├── .env.example
└── README.md
```

---

## 3. Data Layer Design

### 3.1 SQLite Schema

```sql
-- Main jobs table with FTS5 support
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT UNIQUE NOT NULL,
    title         TEXT,
    company       TEXT,
    ats_type      TEXT,
    ats_id        TEXT,
    location      TEXT,
    is_remote     INTEGER,          -- 0/1 boolean
    salary_min    REAL,
    salary_max    REAL,
    salary_currency TEXT,
    salary_period TEXT,
    salary_summary TEXT,
    employment_type TEXT,
    department    TEXT,
    team          TEXT,
    description   TEXT,
    posted_at     TEXT,             -- ISO8601 stored as TEXT
    requisition_id TEXT,
    apply_url     TEXT,
    commitment    TEXT,
    country       TEXT,             -- Extracted from location string
    fetched_cycle TEXT,             -- YYYY-MM-DD of ingestion cycle
    created_at    TEXT DEFAULT (datetime('now'))
);

-- FTS5 virtual table for full-text search on title + company + description
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title,
    company,
    description,
    location,
    content='jobs',
    content_rowid='id'
);

-- Indexes for common filter queries
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at   ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_ats_type    ON jobs(ats_type);
CREATE INDEX IF NOT EXISTS idx_jobs_country     ON jobs(country);
CREATE INDEX IF NOT EXISTS idx_jobs_is_remote   ON jobs(is_remote);
CREATE INDEX IF NOT EXISTS idx_jobs_company     ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched     ON jobs(fetched_cycle);

-- Manifest tracking table
CREATE TABLE IF NOT EXISTS manifest_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,     -- manifest's updated_at value
    total_jobs   INTEGER,
    ats_count    INTEGER,
    status       TEXT               -- 'success' | 'failed' | 'skipped'
);
```

### 3.2 USA Location Filtering Strategy

The `location` field is a raw string from each ATS. No lat/lon in v2.0.
Filter approach — apply all of these at ingest time, store `country` column:

```python
USA_PATTERNS = [
    r'\b(United States|USA|US)\b',
    r',\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|'
    r'MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|'
    r'SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b',
    r'\b(New York|Los Angeles|San Francisco|Chicago|Seattle|Austin|Boston|'
    r'Denver|Atlanta|Miami|Dallas|Houston|Portland|San Diego|Nashville)\b'
]
```

Flag rows matching any pattern as `country = 'US'`.
Rows with `NULL` location or non-US locations are still stored but tagged
`country = NULL` — available for future international expansion.

### 3.3 24-Hour Window Logic

```python
from datetime import datetime, timezone, timedelta

def get_last_24h_jobs(conn):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    query = """
        SELECT * FROM jobs
        WHERE country = 'US'
          AND posted_at >= ?
        ORDER BY posted_at DESC
    """
    return pd.read_sql(query, conn, params=[cutoff.isoformat()])
```

---

## 4. Ingestion Pipeline (`services/ingestion.py`)

### Flow

```
Fetch manifest.json
    → Compare manifest updated_at with last manifest_log entry
    → If same timestamp → skip (already up to date)
    → If new → proceed

For each ATS in USA_ATS_LIST:
    → Download parquet from manifest URL
    → Save to cache/parquet/{ats}.parquet
    → Load into pandas DataFrame
    → Extract country from location string
    → Filter: keep all rows (not just USA — store everything, filter at query time)
    → Upsert into SQLite jobs table (ON CONFLICT(url) DO UPDATE)
    → Rebuild FTS5 index

Log result to manifest_log
```

### Key Design Decisions

- **Upsert on `url`** — applyd uses URL as the unique identifier per job.
  `ON CONFLICT(url) DO UPDATE` ensures no duplicates on re-ingestion.
- **Download per-ATS, not all.parquet** — avoids pulling 264MB when only
  ~150MB of relevant ATS data is needed.
- **Cache parquet files locally** — on re-runs, check parquet SHA256 from
  manifest against locally cached file. Skip download if hash matches.
- **Async downloads** — use `httpx.AsyncClient` to download multiple ATS
  parquet files concurrently (respects the MIT-licensed open dataset).

```python
USA_ATS_LIST = [
    "amazon", "apple", "ashby", "avature", "bamboohr", "breezy",
    "builtin", "cornerstone", "eightfold", "gem", "google", "greenhouse",
    "icims", "jazzhr", "lever", "mercor", "meta", "oracle", "personio",
    "phenom", "pinpoint", "recruiterbox", "recruitee", "rippling",
    "smartrecruiters", "successfactors", "taleo", "teamtailor", "tesla",
    "tiktok", "uber", "wellfound", "weworkremotely", "workable",
    "workday", "ycombinator"
]
# Excluded: bundesagentur, eures, arbetsformedlingen, join_com,
#           jobsch, programathor, manfred, getonbrd, remoteok,
#           thehub, wanted (non-US focused)
```

---

## 5. FastAPI Application

### 5.1 App Entry Point (`app/main.py`)

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from app.scheduler import start_scheduler
from app.database import init_db
from app.routers import dashboard, jobs, stats

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()   # starts APScheduler 24hr cron
    yield

app = FastAPI(title="Applyd", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard.router)
app.include_router(jobs.router, prefix="/api/jobs")
app.include_router(stats.router, prefix="/api/stats")
```

### 5.2 API Endpoints (`routers/jobs.py`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/jobs/` | Paginated job list (default: last 24h, USA, page=1, limit=50) |
| `GET` | `/api/jobs/{id}` | Single job detail |
| `GET` | `/api/jobs/search` | Search with filters (see params below) |
| `GET` | `/api/jobs/ats` | List all available ATS types in DB |
| `GET` | `/api/jobs/companies` | List companies (with job count) |
| `POST` | `/api/jobs/ingest` | Manually trigger ingestion |
| `GET` | `/api/jobs/ingest/status` | Last ingestion status + timestamp |

**Search query params:**

```
GET /api/jobs/search?
    q=salesforce developer     # full-text search
    &location=New York         # location string match
    &country=US                # country filter (US/DE/SE/etc)
    &ats=greenhouse            # ATS type filter
    &remote=true               # remote toggle
    &salary_min=80000          # min salary
    &employment_type=FULL_TIME # employment type
    &posted_hours=24           # posted in last N hours (default 24)
    &page=1                    # pagination
    &limit=50                  # page size (max 100)
    &sort=posted_at_desc       # sort order
```

### 5.3 Stats Endpoints (`routers/stats.py`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats/summary` | Total jobs, last 24h count, ATS breakdown |
| `GET` | `/api/stats/by_ats` | Job counts grouped by ATS |
| `GET` | `/api/stats/by_country` | Job counts grouped by country |
| `GET` | `/api/stats/top_companies` | Top hiring companies (last 24h) |
| `GET` | `/api/stats/salary_range` | Salary distribution data |
| `GET` | `/api/stats/remote_vs_onsite` | Remote vs on-site ratio |

### 5.4 HTML Page Routes (`routers/dashboard.py`)

| Route | Template | Description |
|---|---|---|
| `GET /` | `index.html` | Main dashboard — 24h USA jobs, summary cards |
| `GET /search` | `search.html` | Search/filter page |
| `GET /job/{id}` | `job_detail.html` | Single job page |
| `GET /stats` | `stats.html` | Analytics page with charts |
| `GET /settings` | `settings.html` | Trigger manual refresh, view sync status |

---

## 6. Frontend Design

### Pages

**`/` — Main Dashboard**
- Top summary cards: Total jobs today, USA jobs today, Remote jobs, Top ATS
- Job listing table with lazy loading (infinite scroll via JS Intersection Observer)
- Quick filter bar: ATS dropdown, Remote toggle, Posted time (24h/48h/7d)
- Each job card shows: Title, Company, Location, ATS badge, Posted time, Salary (if available), Apply button

**`/search` — Search Page**
- Full search form: keyword, location, country, ATS, remote, salary range, employment type
- Results table with same lazy loading
- Active filters shown as dismissible badges
- Export results as CSV button

**`/job/{id}` — Job Detail**
- Full job description (rendered from HTML/markdown)
- Salary, location, employment type, department
- ATS source badge
- Direct apply link button
- "Save for MS2" button (future — queue this job for agent)

**`/stats` — Analytics**
- Bar chart: Jobs by ATS (Chart.js)
- Pie chart: Remote vs On-site
- Line chart: Jobs posted over last 7 days
- Table: Top 20 hiring companies
- Salary distribution histogram

### Lazy Loading Strategy

```javascript
// Intersection Observer for infinite scroll
const observer = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !isLoading) {
        loadNextPage();
    }
}, { threshold: 0.1 });

observer.observe(document.getElementById('scroll-sentinel'));

async function loadNextPage() {
    isLoading = true;
    currentPage++;
    const params = new URLSearchParams({ ...activeFilters, page: currentPage, limit: 50 });
    const res = await fetch(`/api/jobs/?${params}`);
    const data = await res.json();
    appendJobCards(data.jobs);
    if (data.jobs.length < 50) observer.disconnect(); // no more pages
    isLoading = false;
}
```

---

## 7. Scheduler (`app/scheduler.py`)

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.services.ingestion import run_ingestion

scheduler = AsyncIOScheduler()

def start_scheduler():
    # Run at 11:00 UTC daily (applyd manifest updates ~10:00 UTC based on
    # manifest generated_at: 2026-05-13T10:09:19Z)
    scheduler.add_job(
        run_ingestion,
        trigger='cron',
        hour=11,
        minute=0,
        id='daily_ingestion',
        replace_existing=True
    )
    # Also run once at startup if DB is empty
    scheduler.add_job(
        run_ingestion_if_empty,
        trigger='date',  # run once immediately
        id='startup_ingestion'
    )
    scheduler.start()
```

---

## 8. Dependencies (`requirements.txt`)

```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0
jinja2>=3.1.4
python-multipart>=0.0.9

# Data
applyd[parquet]>=0.1.0
pandas>=2.0
pyarrow>=15.0
httpx>=0.27

# Database
aiosqlite>=0.20.0

# Scheduler
apscheduler>=3.10.0

# Config
pydantic-settings>=2.0
python-dotenv>=1.0.0
```

---

## 9. Configuration (`.env.example`)

```env
# App
APP_HOST=0.0.0.0
APP_PORT=8000
DEBUG=true

# Data paths
DB_PATH=./data/applyd.db
CACHE_DIR=./cache/parquet

# Ingestion
MANIFEST_URL=https://storage.stapply.ai/jobhive/v1/manifest.json
INGEST_HOUR_UTC=11          # cron hour to run daily refresh
DEFAULT_COUNTRY_FILTER=US   # default country for dashboard

# Pagination
DEFAULT_PAGE_SIZE=50
MAX_PAGE_SIZE=100
```

---

## 10. Development Phases

### Phase 1 — Core Data Pipeline
1. Set up project structure and virtual environment (`uv`)
2. Implement `database.py` — SQLite init, schema, FTS5
3. Implement `manifest.py` — fetch manifest, parse, diff check
4. Implement `ingestion.py` — async parquet download, upsert to SQLite
5. Test: manually trigger ingestion, verify row counts in SQLite
6. Implement `scheduler.py` — APScheduler wired to ingestion

### Phase 2 — API Layer
1. Implement `jobs.py` router — paginated list, search, filters
2. Implement `stats.py` router — summary, by_ats, top_companies
3. Test all endpoints with curl / httpie
4. Validate 24h filter, USA filter, FTS search

### Phase 3 — Frontend
1. Create `base.html` with Bootstrap layout and nav
2. Build `index.html` — summary cards + lazy-loaded job list
3. Build `search.html` — filter form + results
4. Build `job_detail.html` — full job page
5. Build `stats.html` — Chart.js charts wired to `/api/stats/*`
6. Add `dashboard.js` — infinite scroll
7. Add `search.js` — filter form submission + active filter badges

### Phase 4 — Polish
1. Loading states, empty states, error states in UI
2. Manual refresh trigger from settings page
3. Last synced timestamp displayed in nav
4. CSV export on search results
5. "Save for MS2" button stub on job detail (queues job_id to a local table)

---

## 11. Important Technical Notes for Coding Agent

1. **No applyd `search()` for bulk USA pull** — `applyd.search()` requires
   a `query` string and downloads filtered slices. For the dashboard default
   view (all USA jobs, last 24h), bypass `applyd.search()` entirely and
   directly download per-ATS parquet from manifest URLs using `httpx`. Use
   `applyd.search()` only for keyword-based user searches.

2. **FTS5 sync trigger** — After every upsert batch, run:
   ```sql
   INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild');
   ```

3. **Parquet SHA256 check** — Before downloading, compare
   `manifest["by_ats"][ats]["parquet_sha256"]` against locally cached file
   hash. Skip download if identical.

4. **`posted_at` is nullable** — Many ATS sources don't expose post date.
   For the 24h filter, only include rows where `posted_at IS NOT NULL`.
   Handle gracefully — don't crash on NULL posted_at rows.

5. **`description` can be HTML** — Render safely in frontend using
   `DOMPurify.sanitize()` before `innerHTML`. Never raw inject.

6. **`raw` column** — Store as JSON string in SQLite TEXT column.
   Do not index it. It's for MS2 agent reference only.

7. **Manifest `updated_at`** — This is the canonical freshness signal.
   Store it in `manifest_log` and compare before each ingestion run to
   avoid unnecessary re-downloads.

8. **SQLite WAL mode** — Enable WAL for concurrent read/write:
   ```sql
   PRAGMA journal_mode=WAL;
   PRAGMA synchronous=NORMAL;
   ```

9. **Memory** — Loading all 3.8M rows into pandas at once will crash the
   Mac. Always stream parquet in chunks:
   ```python
   import pyarrow.parquet as pq
   pf = pq.ParquetFile('workday.parquet')
   for batch in pf.iter_batches(batch_size=10_000):
       df = batch.to_pandas()
       upsert_batch(df, conn)
   ```

10. **For coding agent**: Start with Phase 1 only. Verify data flows end-to-end
    before touching the frontend. The ingestion pipeline is the foundation
    everything else depends on.
