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

### 5) Expired-job detection lifecycle
- Crowd-sourced "Report broken" affordance on every job card
- Free silent-drop tracking from the daily ingest (`missed_ingest_cycles`)
- Background HTTP verifier with per-ATS body-text matchers (Greenhouse, Lever, Ashby, Workday, iCIMS, Workable, SmartRecruiters, BambooHR, Recruitee, JazzHR) + HTTP-status fallback for the long tail
- Two-signal confidence model: `active → suspected → expired`
- Per-`(ats_type, hour)` circuit breaker so one ATS outage cannot wipe its corpus
- Two-stage kill switches (`expired_detection_enabled`, `verifier_auto_marking_enabled`) for safe rollout
- Three-way Availability filter (Open / All / Closed) with verified-expired hidden by default
- Admin moderation queue + observability dashboard

### 6) Admin panel (`/admin`)
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
  - APScheduler in-process:
      ├─ daily_ingestion          (cron 11:00 UTC)
      ├─ catchup_poll_ingestion   (every 30 min while skipped)
      ├─ verify_suspected         (every 5 min, ≤50 jobs/tick)
      └─ verify_periodic_sweep    (every 10 min, batch sized for 1-day full sweep)
          |
          v
SQLite (
  jobs (incl. verification_status, missed_ingest_cycles, report_count, …),
  saved_jobs, job_reports, job_verification_log,
  verifier_circuit_breaker, user_action_rate_limits,
  manifest_log, scrape_run*, app_maintenance, admin_audit
)
```

### Important data paths
- **Daily manifest path**: scheduled ingestion from upstream
- **Manual local-scraper path**: operator-triggered runs for targeted ATS refreshes
- **Expired-detection path**: user reports + manifest-drop tracking + HTTP verifier → `job_lifecycle` state machine → `jobs.verification_status`

The first two converge into the same `jobs` table through the same upsert contract. The third reads from `jobs` and writes back availability state without producing or removing rows directly (deletes happen only via the unified 30-day prune in `ingestion.prune_old`).

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
| `/admin/job-reports` | Moderation queue for user-submitted "broken job" reports. Filter by status (active / suspected / expired) and min report count. One-click expire / reactivate writes to `admin_audit`. |
| `/admin/expirations` | Two-tab live dashboard. **Live tab**: SSE-pushed counters (active/suspected/expired), 3 activity windows (today / last hour / last 24h), per-ATS and per-detector matrices, recent-checks feed, schedule + kill-switch status, circuit breakers + clear, "Run sweep now" button. **Review & cleanup tab**: filterable expired-jobs table, group stats (by ATS / reason / country / company), bulk delete + bulk reactivate with type-the-count confirmation gate. |
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

GET    /api/admin/job-reports                              # query: status, min_reports, limit, offset
GET    /api/admin/job-reports/{job_id}                     # job + all reports + last 50 verification log rows
GET    /api/admin/job-reports-reporters                    # query: min_reports — flags anomalous reporters
POST   /api/admin/jobs/{job_id}/expire                     # form: csrf_token — admin override → admin_audit
POST   /api/admin/jobs/{job_id}/reactivate                 # form: csrf_token — admin override → admin_audit

# Expirations dashboard (Live + Review tabs)
GET    /api/admin/expirations/summary                      # one-shot snapshot
GET    /api/admin/stream/expirations                       # SSE (5-min bounded, ~5s tick)
GET    /api/admin/expirations/review                       # query: ats, country, detector, reason,
                                                           #        company, expired_after/before,
                                                           #        sort, limit, offset
POST   /api/admin/expirations/bulk-delete                  # form: filters_json, confirm_count, csrf_token
POST   /api/admin/expirations/bulk-reactivate              # form: filters_json, confirm_count, csrf_token
POST   /api/admin/expirations/run-sweep                    # form: csrf_token, batch_size?
POST   /api/admin/verifier/circuit-breaker/{ats_type}/clear  # form: csrf_token — clear tripped breakers
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

## Expired-Job Detection Lifecycle

### The problem we're solving

Users browsing "last 24h" jobs frequently hit ATS pages for positions that are already closed. HTTP status alone is unreliable — Ashby, iCIMS, Workday, Lever (and most ATSes) return **200 OK** with an expiration message rendered in the body. A naive "is this URL alive" check would miss most expirations. A single user complaint is too weak to act on; with no reporting path at all, we miss the crowd signal entirely.

`applyd` runs a multi-signal availability lifecycle on every job. Three independent signals feed a state machine; transitions are gated behind kill switches and a per-ATS circuit breaker so the system fails closed rather than wiping live jobs on a flaky day.

### The state machine

```
                  ┌───────── one weak signal ─────────┐
                  ▼                                    │
   ┌──────────┐    ┌─────────────┐    ┌──────────┐    │
   │  active  │ →  │  suspected  │ →  │ expired  │    │
   └──────────┘    └─────────────┘    └──────────┘    │
        ▲                ▲                   │        │
        │                │                   │        │
        │  HTTP active   │  HTTP 404/410/    │        │
        │  downgrade     │  listing-redirect │        │
        │                └───────────────────┘        │
        │  (HTTP is ground truth — single signal      │
        │   alone, regardless of corroboration)       │
        │                                              │
        └────────── manifest reappearance ─────────────┘
                   (requires 2 clean cycles to prevent flapping)
```

**Transitions in plain English**

| From | Signal | To | Notes |
|---|---|---|---|
| `active` | 1 user report | `suspected` | Yellow "May not be available" pill; still visible |
| `active` | `missed_ingest_cycles ≥ 2` | `suspected` | Counted only on successful (not skipped) ingest cycles |
| `active` | HTTP 404 / 410 / listing-redirect | `expired` | HTTP is ground truth; jumps the suspected stage |
| `active` | matcher body match (`"this job is no longer available"`) | `expired` | Per-ATS matcher confirms |
| `suspected` | 2nd distinct user report **+** `missed_ingest_cycles ≥ 2` | `expired` | Pure user-report storms without corroboration stay suspected |
| `suspected` | HTTP `active` | `active` | Verifier confirmed live, downgrade |
| `expired` | manifest UPSERT touches it again (`missed_ingest_cycles = 0`) | `active` | Logged to `admin_audit`; grace gate prevents flap |

All transitions are gated by **two settings** (both default-safe):

- `expired_detection_enabled` (default `True`) — global on/off
- `verifier_auto_marking_enabled` (default `False`) — when `False`, signals collect and verifier writes to `job_verification_log`, but no row's `verification_status` ever flips to `expired`. Used for the 2-week observation period before flipping the switch in production.

### The three signals

#### 1. Manifest drop (free, runs inside ingestion)

Every UPSERT bumps `jobs.last_seen_in_manifest_at = datetime('now')` and resets `missed_ingest_cycles = 0`. After every **successful** ingest run (`manifest_log.status = 'success'`, never `'skipped'`), one closing UPDATE bumps `missed_ingest_cycles + 1` for every row whose timestamp is older than `cycle_start_iso`:

```sql
UPDATE jobs SET missed_ingest_cycles = missed_ingest_cycles + 1
 WHERE COALESCE(last_seen_in_manifest_at, '') < :cycle_start_iso
```

A row with `missed_ingest_cycles ≥ 2` has been silently dropped from upstream — strong-but-not-conclusive signal. The "skipped cycles don't count" rule prevents the manifest-unchanged days from inflating the counter.

#### 2. User report (`POST /api/jobs/{id}/report`)

Body shape:

```json
{ "reason": "not_found | position_filled | link_broken | other",
  "detail": "optional free text, capped at 280 chars, PII-stripped" }
```

Behaviour:

- **Idempotent per (user, job)**: a UNIQUE constraint on `job_reports(user_id, job_id)` means a user clicking Report twice doesn't double-count.
- **PII scrub**: emails and phone numbers in `detail` are regex-replaced with `[email]` / `[phone]` before storage.
- **Two rate limits** stored in `user_action_rate_limits` (same shape as `auth_rate_limits`):
  - `report_rate_limit_per_day` (default `20`)
  - `report_rate_limit_per_company_per_week` (default `5`)
- **Lifecycle hook**: after insert + `report_count` update, calls `job_lifecycle.on_user_report()` which decides whether to escalate.
- **Withdrawal**: `DELETE /api/jobs/{id}/report` removes the row and decrements the count.

#### 3. HTTP verifier (`app/services/verifier.py`)

Per-ATS dispatch with body-text matchers covering the top providers, plus a conservative HTTP-status fallback for everything else.

**Currently shipped matchers:**

| ATS | Status signals | Body phrases |
|---|---|---|
| `greenhouse` | 404, 410 | `"this job is no longer available"`, `"job you were looking for could not be found"`, redirect to `/jobs` |
| `lever` | 404, 410 | `"this posting is no longer available"`, `"we are no longer accepting applications"` |
| `ashby` | 404, 410 | `"this job is no longer available"`, `"position is no longer accepting applications"` |
| `workday` | — (rarely 404s) | `"this job posting is no longer available"`, `data-automation-id="errormessage"` |
| `icims` | 404, 410 | `"this position is no longer available"`, `"job has been filled"` |
| `workable` | 404, 410 | `"this job is no longer accepting applications"`, `"this job has been filled"` |
| `smartrecruiters` | 404, 410 | `"position has been closed"` |
| `bamboohr` | 404, 410 | `"we're sorry, this job is no longer available"` |
| `recruitee` | 404, 410 | `"vacancy is no longer available"` |
| `jazzhr` | 404, 410 | `"this position has been filled"` |
| **everything else** | 404, 410 only | (no body regex — too noisy across the long tail) |

**Per check:**

1. `HEAD` first — cheap. If `405/501`, fall through to `GET`. If `200/301/302`, also `GET` to inspect the body (HEAD can lie).
2. Run the per-ATS matcher on the lowercased body (capped at 200 KB).
3. Listing-root redirect heuristic (e.g. `boards.greenhouse.io/acme/jobs`) → `expired`.
4. Inconclusive `200 OK` with no expiry signal → assumed `active` (we prefer false-negatives over wrongly hiding live jobs).
5. `429` → set per-host `skip_until = now + 1h`.

**Concurrency:**

- `asyncio.Semaphore` per `ats_type`, default `4` (overridable: `verifier_per_host_concurrency`)
- Global cap `16` (overridable: `verifier_global_concurrency`)
- Polite User-Agent: `applyd-verifier/1.0`

### Scheduler — every job that runs in-process

`app/scheduler.py` registers everything below into one `AsyncIOScheduler`. Two separate `asyncio.Lock`s keep ingest and verifier from blocking each other.

| Job ID | Trigger | Lock | What it does | Skip rule |
|---|---|---|---|---|
| `daily_ingestion` | `CronTrigger(hour=11, minute=0)` UTC | `_ingest_lock` | Pull manifest, parallel-download parquets, UPSERT, prune, FTS rebuild, maybe-VACUUM. After success, fires manifest-drop sweep. | Manifest unchanged → logged `skipped`, no counter increments. |
| `startup_ingestion` | Fires once at app startup | `_ingest_lock` | Catch-up if DB empty OR last successful ingest is from an earlier UTC date. | Otherwise no-op. |
| `catchup_poll_ingestion` | `IntervalTrigger(minutes=30)` | `_ingest_lock` | Re-attempts a skipped 11:00 run. | Only fires inside the configured poll window and only when today has no `success` row yet. |
| `verify_suspected` | `IntervalTrigger(minutes=5)` | `_verify_lock` | `drain_suspected()` — picks up to `verifier_suspected_batch` (default 50) jobs ordered by oldest `verification_status_at`. Re-checks via HTTP. | Skipped when `expired_detection_enabled = False` or lock already held. |
| `verify_periodic_sweep` | `IntervalTrigger(minutes=10)` | `_verify_lock` | `drain_periodic_sweep()` — with `sweep_all_active=True` (default) walks every active job continuously ordered by oldest `last_verified_at`; batch sized so the whole corpus is covered every `verifier_sweep_days` (default 1). | Skipped when disabled or lock held. |
| (post-ingest hook) | Runs at the end of `_run_daily` on success | `_verify_lock` | `drain_manifest_drops()` — calls `job_lifecycle.on_manifest_drop` on every row with `missed_ingest_cycles ≥ 2`, then HTTP-verifies them. | Only after a `success` ingest. |

**Batch math for the periodic sweep:**

```
ticks_per_window = (sweep_days * 24 * 60) / sweep_interval_minutes
batch_size       = ceil(active_corpus / ticks_per_window)
```

So `500,000 active / ((1 day × 24h × 60min) / 10min)` = `500,000 / 144 ≈ 3,470 jobs per 10-minute tick`. Per-host concurrency limits parallelism; a tick that size completes in well under the 10-minute window.

You can override the batch size via `VERIFIER_SWEEP_BATCH_SIZE`, change cadence with `VERIFIER_SWEEP_DAYS=3` (or 5), or change tick frequency with `VERIFIER_SWEEP_INTERVAL_MINUTES=30`.

### Data model additions

**New columns on `jobs`** (additive via `_ensure_column` in `_migrate_schema`):

| Column | Default | Purpose |
|---|---|---|
| `verification_status` | `'active'` | State machine: `active \| suspected \| expired` |
| `verification_status_at` | `NULL` | When it last transitioned (drives the 30-day expired prune cutoff) |
| `last_verified_at` | `NULL` | Last successful HTTP check; drives sweep ordering |
| `last_seen_in_manifest_at` | `datetime('now')` | Bumped by every UPSERT |
| `missed_ingest_cycles` | `0` | Successful cycles since last seen upstream |
| `report_count` | `0` | Denormalized — number of distinct user reports |

**Partial indexes** (created in `_migrate_schema`, never in `SCHEMA_SQL` because the columns don't exist yet on existing DBs when the script runs):

```sql
CREATE INDEX idx_jobs_expired   ON jobs(id) WHERE verification_status = 'expired';
CREATE INDEX idx_jobs_suspected ON jobs(verification_status_at) WHERE verification_status = 'suspected';
CREATE INDEX idx_jobs_drops     ON jobs(id) WHERE missed_ingest_cycles >= 1;
CREATE INDEX idx_jobs_verify_due ON jobs(last_verified_at) WHERE verification_status = 'active';
```

**New tables:**

```sql
job_reports(
  id, user_id → users.id (CASCADE),
  job_id → jobs.id (SET NULL),  -- so abuse-signal survives the 30-day prune
  reason, detail, reported_at,
  UNIQUE(user_id, job_id)
)

job_verification_log(
  id, job_id → jobs.id (CASCADE),
  checked_at, trigger,           -- user_report | manifest_drop | periodic | admin
  http_status, result, detector, detail
)
-- TTL-pruned to 90 days inside ingestion._maybe_vacuum.

verifier_circuit_breaker(
  ats_type, hour_bucket,         -- "YYYY-MM-DDTHH" UTC
  expire_count, tripped_at, cleared_at, cleared_by,
  PRIMARY KEY (ats_type, hour_bucket)
)

user_action_rate_limits(
  bucket_key,                    -- "report:day:<user_id>" or "report:co:<user_id>:<company>"
  count, window_started_at,
  PRIMARY KEY (bucket_key)
)
```

### Pruning policy (deletes from `jobs`)

The 30-day prune is now a **single unified DELETE** with two branches:

```sql
DELETE FROM jobs WHERE
  (verification_status = 'expired'
   AND verification_status_at < datetime('now', '-30 days'))
  OR (verification_status != 'expired'
      AND COALESCE(posted_at, first_seen_at) < datetime('now', :rolling_window))
```

| Rule | Branch | Notes |
|---|---|---|
| Hidden as expired, status flipped >30d ago | expired branch | `saved_jobs` cascade-deletes; `job_reports.job_id` is `SET NULL` so abuse-signal survives |
| Not expired, effective-date outside 30-day rolling window | non-expired branch | Existing behavior, preserved |

`job_verification_log` has its own 90-day TTL pruned inside `_maybe_vacuum`.

### Safeguards

- **Two kill switches** (`expired_detection_enabled`, `verifier_auto_marking_enabled`) — the second one is the critical safety: lets you collect signals + run the verifier while keeping `verification_status='expired'` writes gated for the first weeks in production.
- **Per-`(ats_type, hour_bucket)` circuit breaker** — if a single ATS produces more than `verifier_circuit_breaker_threshold` (default `25`) auto-expirations in one hour, further `expired` writes for that ATS halt until an admin clears the breaker. Protects against the "Workday went down at 2 AM" scenario.
- **Two-signal rule for non-HTTP signals** — a user-report storm without corroboration from manifest drop or HTTP cannot push past `suspected`.
- **Reactivation grace** — `expired → active` requires `missed_ingest_cycles = 0` (the row reappeared in the manifest), and the lifecycle service logs to `admin_audit`.
- **PII strip + length cap** on `job_reports.detail` (`280` chars; email/phone regex-replaced).
- **Rate limits** per user/day and per user/company/week (see `user_action_rate_limits`).
- **Manifest-skip awareness** — `mark_manifest_drops` only fires on `status='success'` cycles, so the daily catch-up poll's repeated `skipped` runs don't drift the counter.

### UI surfaces

**Job cards (`/dashboard`)** — every card in grid and list view has:

- A small "report broken" icon next to Save/Apply
- A yellow `May not be available` badge when `verification_status = 'suspected'`
- A red `Closed` badge when `verification_status = 'expired'`

**Filter sidebar** — new **Availability** section with three buttons:

| Button | Effect | Query string |
|---|---|---|
| Open *(default)* | Hides verified-expired | (none) |
| All | Shows everything including expired | `?include_expired=true` |
| Closed | Shows only verified-expired | `?only_expired=true` |

**Saved jobs (`/saved`)** — expired jobs show a red inline banner ("No longer accepting applications.") and the closed badge. The job row stays in the saved list until either the user removes it or the 30-day prune fires.

**Job detail (`/job/{id}`)** — `verification_status` is included in the API response (`JobDetail`) and surfaces matching badges.

### API surface (user-facing)

```
POST   /api/jobs/{job_id}/report      # body: { reason, detail? }
DELETE /api/jobs/{job_id}/report      # withdraw own report

GET    /api/jobs/?include_expired=true        # show all incl. expired
GET    /api/jobs/?only_expired=true           # show only expired (left-rail "Closed")
```

`JobSummary` and `JobDetail` now include `verification_status` and `is_reported`.

### Configuration reference

All knobs live in `app/config.py` and are overridable as env vars (pydantic `BaseSettings`):

```
EXPIRED_DETECTION_ENABLED=true          # global kill switch
VERIFIER_AUTO_MARKING_ENABLED=false     # second-stage kill switch (auto-promote to expired)
VERIFIER_SWEEP_DAYS=1                   # full-corpus re-check cadence (default: 1 day)
VERIFIER_SWEEP_INTERVAL_MINUTES=10      # how often the sweep ticks (default: 10 min)
VERIFIER_SWEEP_ALL_ACTIVE=true          # true = walk every active job continuously;
                                        # false = only check jobs older than sweep_days
VERIFIER_PER_HOST_CONCURRENCY=4         # per-ATS parallel checks
VERIFIER_GLOBAL_CONCURRENCY=16          # global cap across all ATSes
VERIFIER_REQUEST_TIMEOUT_SECONDS=20     # per-request timeout
VERIFIER_SUSPECTED_INTERVAL_MINUTES=5   # how often to drain the suspected pool
VERIFIER_SUSPECTED_BATCH=50             # max suspected jobs per drain tick
VERIFIER_SWEEP_BATCH_SIZE=              # blank = auto: corpus / (sweep_days * 24h / sweep_interval)
VERIFIER_CIRCUIT_BREAKER_THRESHOLD=25   # max auto-expires per (ats, hour) before halt
REPORT_RATE_LIMIT_PER_DAY=20
REPORT_RATE_LIMIT_PER_COMPANY_PER_WEEK=5
```

### Sweep cadence — when each job is revisited

The periodic sweep is a **rolling priority queue**, not a fixed timer. The SQL ordering is:

```sql
SELECT id, url FROM jobs
 WHERE verification_status = 'active'
 ORDER BY last_verified_at IS NULL DESC,  -- never-checked first
          last_verified_at ASC,            -- oldest-checked next
          id ASC
 LIMIT <batch_size>
```

When a job is checked, `last_verified_at = now` is stamped on its row, sinking it to the bottom of the queue. The natural rotation produces these numbers under defaults (`sweep_days=1`, `sweep_interval=10min`):

```
ticks per day        = 144
jobs per tick        = active_corpus / 144
time to full pass    = ~24 hours
revisit interval     ≈ verifier_sweep_days   (1 day)
```

So a job checked at 10:00 today is back at the top of the queue around 10:00 tomorrow. Three events cut the line and revisit a job sooner:

| Event | Visited within |
|---|---|
| User reports the job | next 5-min `verify_suspected` tick |
| Job missed ≥2 successful ingest cycles | post-ingest `verify_manifest_drops` sweep |
| Admin clicks "Run sweep now" | that batch (`/api/admin/expirations/run-sweep`) |

### Expirations admin dashboard — `/admin/expirations`

Two tabs, both live-updated over SSE (`/api/admin/stream/expirations`, ~5 s tick, 5-min bounded with auto-reconnect):

**Tab 1 — Live.** Schedule + kill-switch panel · status counters · 3 activity windows (today/last-hour/last-24h) · per-ATS matrix · per-detector matrix · last-30 checks feed · circuit breakers + clear · "Run sweep now" trigger.

**Tab 2 — Review & cleanup.** Filter bar (ATS, country, detector, reason, company, date range) · four group-stats cards (by ATS / by reason / by country / top-20 companies) · paginated jobs table with per-row "Reactivate" · bulk-action toolbar.

**Bulk operations safety gate.** Clicking *Delete N* or *Reactivate N* opens a modal that summarises the affected scope ("234 jobs across 4 ATSes and 2 countries") and disables the button until you **type the exact preview count**. Numbers can't be guessed — same gate the backup-restore page uses for filenames. Every bulk operation writes to `admin_audit` with the full filter blob, and the type-the-count check is re-validated server-side against a fresh count just before the DELETE so an admin can't get racing rows.

Behaviour on bulk delete:

- `saved_jobs` FK cascades — entries that referenced the deleted job are removed.
- `job_reports.job_id` is `SET NULL` — the report row survives so abuse signal is preserved.
- `job_verification_log` is `ON DELETE CASCADE` — log rows for the deleted job are removed too.

### Operating the verifier

**Start it:** the verifier runs in-process with the dashboard. No separate command.

```bash
make run
```

Startup log line confirms state:

```
scheduler started (daily cron at 11:00 UTC; catch-up poll every 30m until 17:59 UTC on skipped days; verifier enabled)
```

**Each tick logs a one-line summary:**

```
suspected verifier tick: {'checked': 0}
periodic sweep tick:     {'checked': 3470, 'active': 3320, 'expired': 60, 'error': 90}
post-ingest manifest-drop sweep: {'checked': 412, 'active': 350, 'expired': 40, 'error': 22}
```

`checked` is the batch size for that tick, not the cumulative count.

**Heads up on `make run` and `--reload`.** `make run` starts uvicorn with `--reload`. The file watcher restarts the worker on any code change, which **resets APScheduler** — any verifier tick that hadn't yet fired in the current process is lost. For long observation runs (overnight, multi-hour), run without reload:

```bash
cd dashboard
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Drive one batch from the CLI** — bypasses APScheduler entirely; the most direct way to prove the verifier works against your real corpus:

```bash
cd dashboard
uv run python -m app.cli verify-now --mode sweep --batch 30      # active corpus
uv run python -m app.cli verify-now --mode suspected --batch 50  # suspected pool
uv run python -m app.cli verify-now --mode drops --batch 100     # manifest-drop sweep
```

**Watch progress live:**

```bash
# total checks since the server started
sqlite3 dashboard/data/applyd.db \
  "SELECT COUNT(*), MAX(checked_at) FROM job_verification_log;"

# breakdown by result, last hour
sqlite3 dashboard/data/applyd.db "
  SELECT result, COUNT(*) FROM job_verification_log
   WHERE checked_at >= datetime('now', '-1 hour')
   GROUP BY result;
"

# which matchers fired
sqlite3 dashboard/data/applyd.db "
  SELECT detector, result, COUNT(*) FROM job_verification_log
   WHERE checked_at >= datetime('now', '-24 hours')
   GROUP BY detector, result ORDER BY 3 DESC;
"

# the last 10 checks with detail
sqlite3 dashboard/data/applyd.db "
  SELECT job_id, trigger, http_status, result, detector, detail, checked_at
    FROM job_verification_log ORDER BY id DESC LIMIT 10;
"
```

Or visit [http://localhost:8000/admin/expirations](http://localhost:8000/admin/expirations) for the same counters in the UI, plus tripped-breaker management.

**Manually queue a moderation action** (admin):

```bash
# Force-expire (writes admin_audit)
curl -X POST http://localhost:8000/api/admin/jobs/123/expire \
  -H "Cookie: applyd_session=…" \
  -F csrf_token=…

# Force-reactivate
curl -X POST http://localhost:8000/api/admin/jobs/123/reactivate \
  -H "Cookie: applyd_session=…" \
  -F csrf_token=…

# Clear all tripped breakers for one ATS
curl -X POST http://localhost:8000/api/admin/verifier/circuit-breaker/greenhouse/clear \
  -H "Cookie: applyd_session=…" \
  -F csrf_token=…
```

### Common diagnostics

| Symptom | Likely cause | Fix |
|---|---|---|
| Startup log says `verifier disabled` | `EXPIRED_DETECTION_ENABLED=false` in env | Unset / set to `true` |
| Ticks log but always `checked: 0` | `active` corpus empty (fresh DB without ingest) | `make ingest` |
| Counters climb but no jobs go expired | `verifier_auto_marking_enabled=False` (default) | `VERIFIER_AUTO_MARKING_ENABLED=true make run` once you've reviewed logs |
| `error` count climbs fast | Network / SSL / DNS issue or ATS rate-limiting | Inspect `job_verification_log.detail`; per-host backoff handles 429 automatically |
| Whole ATS keeps hitting the breaker | Threshold too low for that ATS, or upstream outage | Raise `VERIFIER_CIRCUIT_BREAKER_THRESHOLD`; clear breakers via `/admin/expirations` once root cause is known |

---

## Operational Notes

- Manual scrapes do **not** prune old rows
- Dedup/upsert is URL-based
- Empty-scrape safeguards protect existing ATS data from accidental wipe patterns
- Single-flight run enforcement prevents concurrent manual scrape runs
- Expired-job lifecycle defaults to safe-collect mode (`VERIFIER_AUTO_MARKING_ENABLED=false`) so a fresh deployment cannot wrongly mass-hide live jobs
- The verifier scheduler shares **no lock** with daily ingestion — both can run concurrently; SQLite WAL + `busy_timeout=30000ms` absorbs contention
- `job_verification_log` is the source of truth for "how is the verifier doing"; `job_reports` is the source of truth for crowd signal and abuse review

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
