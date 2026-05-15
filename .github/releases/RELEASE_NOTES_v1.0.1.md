## Version
`v1.0.1` - 2026-05-15

## Highlights
- New **Admin Panel** at `/admin` — health, sessions, failed-logins, rate-limits, maintenance mode, backups, audit log.
- New cross-service **admin API** under `/api/admin/*` on both the dashboard and the identity-service, role-gated.
- Live admin overview via **Server-Sent Events** (`/api/admin/stream/health`) with automatic polling fallback.
- Runtime-editable **rate-limit policy** (per-(IP, email), per-email, per-IP, window, lockout) — no more redeploy to tune thresholds.

## Added
- **Admin role pipeline.** `/api/auth/verify` now returns `{ user_id, email, role }`; the dashboard auth middleware writes `role` onto `request.state` for handlers and templates to consume.
- **Identity-service CLI** (`uv run python -m app.cli {init-db,set-role,list-users}`) for one-off admin promotion without touching SQL.
- **`require_admin_user()` FastAPI dependency** (`dashboard/app/admin/deps.py`) and matching `AdminUser` dataclass.
- **Admin HTML pages**: `/admin`, `/admin/sessions`, `/admin/auth-log`, `/admin/rate-limits`, `/admin/maintenance`, `/admin/backups`, `/admin/audit`, plus a chrome'd 404 catchall for unknown `/admin/<path>`.
- **Admin JSON API on the dashboard**: sessions terminate, failed-logins clear, rate-limit policy + unlock, maintenance enable/disable, backups list/create/delete/download/restore, audit listing, ingest + VACUUM, and the SSE stream.
- **Admin JSON API on the identity-service**: `/api/admin/sessions[…/terminate]`, `/api/admin/failed-logins[…/clear]`, `/api/admin/rate-limits[…/policy, …/unlock]`, `/api/admin/users[…/{id}/role]`.
- **Atomic SQLite backups** via the online `.backup()` API for both `applyd.db` and `identity.db`.
- **Token-gated backup download** (`POST` with `APPLYD_BACKUP_TOKEN`) and **safety-snapshotted restore** (4 gates: admin cookie + token + maintenance mode ON + filename retype).
- **Site-wide maintenance mode** with a public 503 page and a middleware that lets admins through so the flag can always be flipped off.
- **`admin_audit` table** plus `audit.record()` service — every state-changing admin action is logged (admin, action, target, IP, user-agent, detail) and best-effort so a DB hiccup never blocks the action it logs.
- **Conditional Admin nav icon** in the dashboard header — only rendered when middleware resolved `role=admin`.
- **Tests**: 31 new identity-service tests (admin API + CLI) and 84 new dashboard tests (deps, audit, maintenance, backups, routers, pages, SSE).

## Changed
- **Dashboard auth middleware** now parses the verify response body (used to only check status code). Cookie-based session checks still piggyback the existing identity-service call — one HTTP hop per request.
- **Middleware ordering**: maintenance middleware is registered before auth so it ends up *inside* auth on the inbound path. This lets it read `request.state.user_role` set by auth. Source comments call this out so it isn't accidentally reordered.
- **Rate-limit thresholds** now read from a `auth_policy` row at runtime, falling back to env-config defaults when unset.
- **Dashboard pytest config**: added `testpaths = ["tests"]` so bare `pytest` (the release-workflow invocation) no longer walks into `vendor/`.
- **`tests/conftest.py`** for the dashboard now exposes `admin_client` and `anon_client` fixtures alongside the existing `client`.

## Fixed
- `make test` and the release-workflow `uv run --group dev pytest` invocations both now collect only `dashboard/tests/`; the prior bare-`pytest` form silently descended into vendored upstream tests.

## Removed
- Nothing removed.

## Upgrade Notes
- **Database / schema changes**:
  - Dashboard SQLite: new `admin_audit` table (auto-created on startup via `init_db()`).
  - Identity-service SQLite: new `auth_policy` table + new index `idx_auth_events_success` (also auto-created).
  - No data migration required; both schemas are forward-compatible with existing rows.
- **New env vars**:
  - `APPLYD_BACKUP_TOKEN` (optional) — required only if you intend to download or restore database backups from `/admin/backups`. Without it the **Backups** page still lets you list/create/delete; download and restore return 503.
- **One-time commands to run**:
  ```bash
  # 1. Pull deps for both services
  make setup

  # 2. Promote yourself to admin (one-time)
  cd identity-service
  uv run python -m app.cli set-role <your-email> admin

  # 3. (Optional) export the backup token before starting the dashboard
  export APPLYD_BACKUP_TOKEN="$(openssl rand -hex 32)"
  ```

## Dashboard Impact
- **`/admin/*`**: new admin section, only visible to users with `role='admin'`. The header gains an "Admin" icon for those users.
- Search/filter behavior: unchanged.
- Saved jobs workflow: unchanged.
- Scrape operations (`/scrape`) changes: unchanged user-facing; admins now have an additional **Ingest now / Force re-ingest** entry point on `/admin`.
- Stats/analytics changes: unchanged. The admin overview surfaces aggregate health (DB size, reclaimable bytes, jobs total, active scrape runs, last successful ingest) via SSE.

## Data & Freshness
- ATS sources added/removed/updated: none.
- Retention/freshness logic changes: none.
- Backfill/reingest required: **No**.

## Known Issues
- Identity-service `set-role` CLI does not refuse to demote the operator's own account (the HTTP API does). Be deliberate when running it.
- `/admin/audit` viewer is read-only; admin-audit CSV export is on the catalog but not in this release.

## Artifacts
- Source code only.

## Verification
- [x] Tests pass locally: `cd identity-service && uv run --group dev pytest` (**53 passed**); `cd dashboard && uv run --group dev pytest` (**114 passed, 1 skipped**).
- [x] Manual smoke: `/` redirects to signin · `/admin/*` redirects anon to signin · `/api/admin/*` returns JSON 401 to anon · admin login surfaces the Admin nav icon.
- [ ] Promote your operator account to admin after upgrade.

## Links
- Compare: https://github.com/Manishrdy/applyd/compare/v1.0.0...v1.0.1
- PRs included: (single commit on `main` for this release)
- Issues resolved: n/a
