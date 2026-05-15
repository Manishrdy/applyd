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

### MS1: Dashboard Service (active)
A FastAPI + Jinja2 application for:

- Searching jobs across ATS providers
- Filtering by recency, country, salary, employment type, remote, etc.
- Saving jobs for later action
- Running local scraper jobs with live progress, logs, and run history
- Admin panel for system health, sessions, rate limits, maintenance mode, backups, and audit log (role-gated)

### Identity Service (active)
A separate FastAPI service for:

- Landing page
- Sign in / Sign up / Logout
- Session issuance and verification (cookie-based, Argon2id + server-side pepper)
- Per-IP / per-email / per-(IP, email) rate limiting with runtime-tunable policy
- Role-gated admin API (`/api/admin/*`) for sessions, failed logins, rate limits, and user role management
- Redirecting authenticated users into dashboard

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

### 5) Admin panel (`/admin`)
- Live health overview via Server-Sent Events with polling fallback
- Active-session viewer and per-session termination
- Failed-login viewer with scoped or broad rate-limit clear
- Runtime-editable rate-limit policy + locked-bucket unlock
- Site-wide maintenance-mode toggle (admins bypass, everyone else gets a 503 page)
- Backup management: atomic SQLite `.backup()` snapshots, token-gated download, safety-snapshotted restore
- Tamper-evident admin audit log for every state-changing action
- Catchall 404 for unknown `/admin/*` paths so misroutes never leak to the public 404

---

## Architecture at a glance

```text
Identity Service (:8100)
  - Landing / Auth pages
  - users + auth_sessions + auth_events + auth_rate_limits + auth_policy
  - /api/auth/verify  → { authenticated, user_id, email, role }
  - /api/admin/*      → sessions, failed-logins, rate-limits, users
          ^
          | (cookie forwarded by dashboard's HTTP client)
          v
Dashboard Service (:8000)
  - Protected UI + APIs
  - auth middleware writes user_id / email / role onto request.state
  - maintenance middleware 503s non-admins when the flag is on
  - /admin/*       → admin pages (HTML)
  - /api/admin/*   → admin JSON API + SSE stream
          |
          v
SQLite (jobs, saved_jobs, manifest_log, scrape_run*, app_maintenance, admin_audit)
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
├─ identity-service/          # Landing + auth (FastAPI + Jinja2)
├─ agent/                     # MS2 (planned)
└─ README.md
```

---

## Make Commands (Quick Reference)

Run these from the repo root. `make help` prints the same list inside the terminal.

| Command | When to use it | What it does |
| --- | --- | --- |
| `make setup` | Fresh clone, or whenever a `pyproject.toml` changes | Installs Python deps for both services (`uv sync`) |
| `make setup-identity` | Only identity-service deps changed | Installs identity-service deps only |
| `make setup-dashboard` | Only dashboard deps changed | Installs dashboard deps only |
| `make init` | Once after `setup`, or after wiping `dashboard/data/` | Creates the dashboard SQLite DB and schema |
| `make ingest` | After `init`, or whenever you want fresh job data | Runs one upstream ingestion cycle into the dashboard DB |
| `make dev` | Bootstrapping a brand-new clone end-to-end | Runs `setup` → `init` → `ingest` in order |
| `make run-dashboard` (alias: `make run`) | Day-to-day — to start the dashboard | Starts dashboard on `:8000` with `--reload` |
| `make run-identity` | Day-to-day — second terminal, for landing/auth | Starts identity-service on `:8100` with `--reload` |
| `make build-css` | After adding/renaming a Tailwind class in any template | Rebuilds `dashboard/static/css/app.css` and mirrors it to `identity-service/` |
| `make watch-css` | While iterating heavily on UI | Rebuilds dashboard CSS on file changes (run `make build-css` once at the end to mirror into identity-service) |
| `make test` | Before pushing changes | Runs the dashboard pytest suite |
| `make clean` | Rarely | Clears pytest cache |

**Typical workflows:**

- *Fresh clone:* `make dev`
- *Pulled new code, deps may have changed:* `make setup`
- *Working on the UI:* terminal 1 → `make run-dashboard` · terminal 2 → `make run-identity` · terminal 3 → `make watch-css`
- *Added a new Tailwind class:* `make build-css` (or rely on `watch-css` if it's running)
- *Want fresher job data:* `make ingest`

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

Or from repo root:

```bash
make setup
```

### 4) Configure environment

```bash
cp .env.example .env
```

Update `.env` as needed for your machine. For local development, defaults are typically enough.

If you plan to use the admin **Backups** page (download or restore database snapshots), set a backup token in the dashboard's environment:

```bash
export APPLYD_BACKUP_TOKEN="$(openssl rand -hex 32)"
```

When unset, the admin UI shows a warning and the download/restore endpoints return 503. Listing, creating, and deleting backups still work without a token.

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

Or from repo root:

```bash
make init
make ingest
```

What this does:

- Creates SQLite schema
- Checks ATS company catalog freshness
- Ingests upstream jobs into local DB
- Prints health/stats summary

### 7) Run identity + dashboard services

```bash
make run-identity
```

In another terminal:

```bash
make run-dashboard
```

Open:

- Identity (landing/auth): [http://localhost:8100](http://localhost:8100)
- Dashboard (protected): [http://localhost:8000/dashboard](http://localhost:8000/dashboard)

Or from repo root:

```bash
make run
```

Runtime flow in browser:

1. `http://localhost:8100/` -> landing page
2. `http://localhost:8100/signin` or `/signup` -> auth
3. Successful login/signup redirects to `http://localhost:8000/dashboard`

### 8) Promote yourself to admin (one-time)

After signing up, grant your account the admin role so `/admin` becomes available. Run this from `identity-service/`:

```bash
cd identity-service
uv run python -m app.cli list-users
uv run python -m app.cli set-role you@example.com admin
```

The dashboard header shows an "Admin" icon for admin users only. Hit [http://localhost:8000/admin](http://localhost:8000/admin) once promoted. Non-admin users hitting `/admin/*` get a 403; signed-out users are redirected to signin.

To demote later, run `set-role you@example.com user`. The CLI refuses to demote a user whose ID matches yours through the admin API, but the CLI has no such guard — be careful not to lock yourself out.

### 9) Trigger ingest manually (optional)

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
- Admins can also trigger ingestion from `/admin` — every admin-triggered action is recorded in `admin_audit`.

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

## Admin Panel

Available to users with `role = 'admin'` only. See the setup step above for how to promote a user.

### Pages (HTML, served by the dashboard)

| Route | Purpose |
|---|---|
| `/admin` | Live health overview — DB size, reclaimable bytes, total jobs, active scrape runs, ingestion status, maintenance state. Streams via SSE; falls back to polling. |
| `/admin/sessions` | Every active session across all users. One-click terminate. |
| `/admin/auth-log` | Recent failed-login events. Scoped or broad rate-limit clear. |
| `/admin/rate-limits` | Edit per-(IP, email), per-email, per-IP attempt thresholds + window/lockout seconds. View and unlock currently locked-out buckets. |
| `/admin/maintenance` | Toggle site-wide maintenance mode and set the message non-admins see. |
| `/admin/backups` | Atomic SQLite backups for both DBs; token-gated download; safety-snapshotted restore. |
| `/admin/audit` | Every admin action with admin, action, target, IP, user-agent, and detail. |

### JSON API (under `/api/admin/`)

Mirrors each page. All endpoints require an admin session cookie and a valid CSRF token for state-changing requests:

```
GET    /api/admin/health                                   # one-shot snapshot
GET    /api/admin/stream/health                            # SSE stream (5-min bounded)
POST   /api/admin/vacuum                                   # SQLite VACUUM
POST   /api/admin/ingest                                   # trigger ingestion

GET    /api/admin/sessions
POST   /api/admin/sessions/{public_id}/terminate

GET    /api/admin/failed-logins
POST   /api/admin/failed-logins/clear                      # body: email?, ip_address?

GET    /api/admin/rate-limits
POST   /api/admin/rate-limits/policy                       # body: pair_max, email_max, ip_max, window_seconds, lockout_seconds
POST   /api/admin/rate-limits/unlock                       # body: bucket_key

GET    /api/admin/maintenance
POST   /api/admin/maintenance/enable                       # body: message
POST   /api/admin/maintenance/disable

GET    /api/admin/backups
POST   /api/admin/backups                                  # body: source=dashboard|identity
POST   /api/admin/backups/{source}/{filename}/delete
POST   /api/admin/backups/{source}/{filename}/download     # body: token  (returns the .db file)
POST   /api/admin/backups/{source}/{filename}/restore      # body: token, confirm=<filename>

GET    /api/admin/audit                                    # query: action, target, limit
```

The identity-service exposes a parallel admin surface for primitives it owns (sessions, failed logins, rate-limit policy, users + role changes):

```
GET    /api/admin/sessions
POST   /api/admin/sessions/{public_id}/terminate
GET    /api/admin/failed-logins
POST   /api/admin/failed-logins/clear
GET    /api/admin/rate-limits
POST   /api/admin/rate-limits/policy
POST   /api/admin/rate-limits/unlock
GET    /api/admin/users
POST   /api/admin/users/{user_id}/role                     # body: role=user|admin
```

### Safety properties worth knowing

- **Audit log is best-effort, never blocking.** A DB outage on the audit insert logs an error and lets the action complete — losing one audit row is preferable to refusing a vacuum or a maintenance toggle.
- **Backups are atomic.** Created via SQLite's online `.backup()` API, so live writers cannot corrupt the snapshot.
- **Restore has four gates:** admin session + valid `APPLYD_BACKUP_TOKEN` + maintenance mode ON + you must re-type the exact filename in the confirm field. The pre-restore live DB is itself snapshotted before the swap, so the prior state is recoverable through the same restore flow.
- **Maintenance middleware** sits inside the auth middleware so it can read `request.state.user_role`. Admins bypass; everyone else gets a 503 with the configured message. Static assets, `/api/health`, and the admin panel itself are always exempt so you can flip the flag back off.
- **SSE stream lifetime is bounded** (default 5 minutes). The browser auto-reconnects, and re-auth happens on reconnect — no stale-session footgun.

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
- Dashboard behavior changes (`/`, `/saved`, `/scrape`, `/stats`, `/settings`, `/admin/*`)
- Data freshness/retention changes
- Any schema/env var/upgrade steps (e.g. `APPLYD_BACKUP_TOKEN`, new admin role requirements)

---

## License

MIT (this repository).  
Built on top of [ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT).  
Upstream jobhive dataset and pipeline are MIT-licensed.
