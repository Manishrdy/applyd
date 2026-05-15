"""SQLite database setup: schema, FTS5, WAL, connection helper."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import settings

log = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    url                   TEXT UNIQUE NOT NULL,
    title                 TEXT,
    company               TEXT,
    ats_type              TEXT,
    ats_id                TEXT,
    location              TEXT,
    is_remote             INTEGER,
    salary_min            REAL,
    salary_max            REAL,
    salary_currency       TEXT,
    salary_period         TEXT,
    salary_summary        TEXT,
    employment_type       TEXT,
    department            TEXT,
    team                  TEXT,
    description           TEXT,
    posted_at             TEXT,
    requisition_id        TEXT,
    apply_url             TEXT,
    commitment            TEXT,
    country               TEXT,
    salary_min_usd_annual REAL,
    salary_max_usd_annual REAL,
    fetched_cycle         TEXT,
    -- first_seen_at: when WE first observed this URL. Acts as a fallback
    -- when upstream `posted_at` is NULL (workday, faang custom APIs, etc.).
    -- Set on INSERT via DEFAULT; preserved by UPSERT (not in DO UPDATE list).
    first_seen_at         TEXT DEFAULT (datetime('now')),
    updated_at            TEXT DEFAULT (datetime('now'))
);

-- effective_date = COALESCE(posted_at, first_seen_at) — used for time-window
-- filters, sorts, and the 45-day prune.

CREATE INDEX IF NOT EXISTS idx_jobs_posted_at        ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS idx_jobs_country_posted   ON jobs(country, posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_ats_type         ON jobs(ats_type);
CREATE INDEX IF NOT EXISTS idx_jobs_company          ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_is_remote        ON jobs(is_remote);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched_cycle    ON jobs(fetched_cycle);
CREATE INDEX IF NOT EXISTS idx_jobs_salary_min_usd   ON jobs(salary_min_usd_annual);
CREATE INDEX IF NOT EXISTS idx_jobs_employment_type  ON jobs(employment_type);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen        ON jobs(first_seen_at);

-- Expression indexes on the effective_date — every API endpoint filters/sorts
-- on COALESCE(posted_at, first_seen_at), so without these SQLite can't use an
-- index for time-window filters or COALESCE-based ORDER BY.
CREATE INDEX IF NOT EXISTS idx_jobs_eff_date          ON jobs(COALESCE(posted_at, first_seen_at));
CREATE INDEX IF NOT EXISTS idx_jobs_country_eff       ON jobs(country, COALESCE(posted_at, first_seen_at) DESC);

-- Composite indexes for facet GROUP BY (Phase 7 perf pass).
-- Without these the ats/employment_type facets do a TEMP B-TREE GROUP BY
-- on ~500K filtered rows (~1.2s on day-0). With them: 50ms.
CREATE INDEX IF NOT EXISTS idx_jobs_country_ats_eff   ON jobs(country, ats_type, COALESCE(posted_at, first_seen_at) DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_country_emp_eff   ON jobs(country, employment_type, COALESCE(posted_at, first_seen_at));

-- Salary range bucket query: scan country-narrowed rows and bucket by
-- salary_max_usd_annual. Was 1.7s, now 13ms.
CREATE INDEX IF NOT EXISTS idx_jobs_country_salary    ON jobs(country, salary_max_usd_annual);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, company, description, location,
    content='jobs',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS saved_jobs (
    job_id     INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    saved_at   TEXT DEFAULT (datetime('now')),
    notes      TEXT,
    status     TEXT DEFAULT 'queued'    -- queued | applied | skipped | archived
);

CREATE INDEX IF NOT EXISTS idx_saved_status ON saved_jobs(status);

CREATE TABLE IF NOT EXISTS manifest_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at            TEXT NOT NULL,
    manifest_updated_at   TEXT NOT NULL,
    total_jobs_upstream   INTEGER,
    ats_count             INTEGER,
    rows_ingested         INTEGER,
    rows_pruned           INTEGER,
    status                TEXT NOT NULL,    -- success | failed | skipped
    error                 TEXT,
    duration_seconds      REAL
);

CREATE INDEX IF NOT EXISTS idx_manifest_log_fetched ON manifest_log(fetched_at DESC);

-- Manual local-scraper runs (LocalScraperSource). Distinct from manifest_log,
-- which is for the daily jobhive cron path. Manual scrape never prunes; the
-- upsert path is the same (ON CONFLICT(url) DO UPDATE on jobs.url).
CREATE TABLE IF NOT EXISTS scrape_run (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    status                TEXT NOT NULL,  -- queued | running | succeeded | partial | failed | cancelled
    ats_requested         TEXT NOT NULL,  -- JSON array of ATS names
    triggered_by          TEXT NOT NULL,  -- manual_ui | manual_api | cli
    scraper_version       TEXT,           -- commit SHA from VENDORED_FROM at run time
    max_companies_per_ats INTEGER,        -- bound applied for this run (NULL = unbounded)
    incremental_enabled    INTEGER DEFAULT 0,
    incremental_days       INTEGER,
    preset_id              INTEGER,
    total_scraped         INTEGER DEFAULT 0,
    total_failed          INTEGER DEFAULT 0,
    total_written         INTEGER DEFAULT 0,
    total_inserted        INTEGER DEFAULT 0,
    total_updated         INTEGER DEFAULT 0,
    error                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_run_started ON scrape_run(started_at DESC);

-- DB-level single-flight: at most one queued/running row exists at any time.
-- Indexing the constant 1 (not `status`) is what enforces single-row: every
-- row matching the WHERE clause would index the same value, so two rows
-- collide regardless of whether their statuses differ (queued vs running).
-- v1 indexed `status` itself which silently allowed queued+running pairs.
DROP INDEX IF EXISTS idx_scrape_run_active;
CREATE UNIQUE INDEX IF NOT EXISTS idx_scrape_run_active_v2
    ON scrape_run((1))
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS scrape_run_ats (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                   INTEGER NOT NULL REFERENCES scrape_run(id) ON DELETE CASCADE,
    ats                      TEXT NOT NULL,
    status                   TEXT NOT NULL,  -- pending | running | succeeded | failed | skipped | cancelled
    started_at               TEXT,
    finished_at              TEXT,
    companies_total          INTEGER DEFAULT 0,
    companies_succeeded      INTEGER DEFAULT 0,
    companies_failed         INTEGER DEFAULT 0,
    rows_scraped             INTEGER DEFAULT 0,
    rows_failed              INTEGER DEFAULT 0,
    rows_written             INTEGER DEFAULT 0,
    rows_inserted            INTEGER DEFAULT 0,
    rows_updated             INTEGER DEFAULT 0,
    rows_skipped_safeguard   INTEGER DEFAULT 0,  -- non-zero if empty-result safeguard tripped
    selected_companies       INTEGER DEFAULT 0,
    phase                    TEXT DEFAULT 'pending',
    phase_started_at         TEXT,
    eta_seconds              INTEGER,
    throughput_cpm           REAL,
    error                    TEXT,
    log_path                 TEXT,
    UNIQUE(run_id, ats)
);

CREATE INDEX IF NOT EXISTS idx_scrape_run_ats_run ON scrape_run_ats(run_id);

-- Per-run URL snapshot. Captures the exact set of jobs.url values this run's
-- parquet contained at load time, so the /scrape/runs/{id} per-ATS drill-down
-- can show the right rows regardless of subsequent writes (manifest cron,
-- later manual runs) bumping jobs.updated_at on the same URLs.
-- ON DELETE CASCADE drops these alongside the run when retention prunes.
CREATE TABLE IF NOT EXISTS scrape_run_url (
    run_id     INTEGER NOT NULL REFERENCES scrape_run(id) ON DELETE CASCADE,
    ats        TEXT NOT NULL,
    url        TEXT NOT NULL,
    PRIMARY KEY (run_id, url)
);

CREATE INDEX IF NOT EXISTS idx_scrape_run_url_run ON scrape_run_url(run_id);
CREATE INDEX IF NOT EXISTS idx_scrape_run_url_run_ats ON scrape_run_url(run_id, ats);

-- Per-company scrape state for fair rotation + incremental targeting.
CREATE TABLE IF NOT EXISTS scrape_company_state (
    ats               TEXT NOT NULL,
    slug              TEXT NOT NULL,
    name              TEXT,
    source_url        TEXT,
    last_scraped_at   TEXT,
    last_run_id       INTEGER REFERENCES scrape_run(id) ON DELETE SET NULL,
    last_status       TEXT,   -- succeeded | failed
    success_count     INTEGER DEFAULT 0,
    failure_count     INTEGER DEFAULT 0,
    total_rows_scraped INTEGER DEFAULT 0,
    PRIMARY KEY (ats, slug)
);
CREATE INDEX IF NOT EXISTS idx_scrape_company_state_ats_last
    ON scrape_company_state(ats, last_scraped_at);

-- Per-ATS round-robin cursor into source CSV ordering.
CREATE TABLE IF NOT EXISTS scrape_ats_cursor (
    ats               TEXT PRIMARY KEY,
    next_index        INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Saved run presets for the Scrape UI.
CREATE TABLE IF NOT EXISTS scrape_preset (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    name                   TEXT NOT NULL UNIQUE,
    ats_requested          TEXT NOT NULL, -- JSON array
    max_companies_per_ats  INTEGER,
    incremental_enabled    INTEGER NOT NULL DEFAULT 0,
    incremental_days       INTEGER,
    notes                  TEXT,
    is_default             INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scrape_preset_default ON scrape_preset(is_default);

-- Internal maintenance markers (vacuum cadence, etc.).
CREATE TABLE IF NOT EXISTS app_maintenance (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Admin audit log. Every state-changing admin action records here.
-- admin_user_id is a loose FK to identity-service users.id (no enforcement;
-- the two DBs live in separate SQLite files).
CREATE TABLE IF NOT EXISTS admin_audit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id  INTEGER NOT NULL,
    admin_email    TEXT NOT NULL,
    action         TEXT NOT NULL,
    target         TEXT,
    detail         TEXT,
    ip_address     TEXT,
    user_agent     TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action  ON admin_audit(action, created_at DESC);

"""

IDENTITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    public_id    TEXT NOT NULL UNIQUE,
    token        TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    ip_address   TEXT,
    user_agent   TEXT,
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at ON auth_sessions(expires_at);

CREATE TABLE IF NOT EXISTS auth_rate_limits (
    bucket_key         TEXT PRIMARY KEY,
    failed_attempts    INTEGER NOT NULL,
    window_started_at  TEXT NOT NULL,
    locked_until       TEXT
);

CREATE TABLE IF NOT EXISTS auth_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    email       TEXT,
    user_id     INTEGER,
    ip_address  TEXT,
    user_agent  TEXT,
    success     INTEGER NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_auth_events_created_at ON auth_events(created_at);
CREATE INDEX IF NOT EXISTS idx_auth_events_success ON auth_events(success, created_at);

CREATE TABLE IF NOT EXISTS auth_policy (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  TEXT
);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else settings.db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    # Give concurrent writers time to finish before failing fast with
    # "database is locked" during startup/background maintenance.
    conn = sqlite3.connect(str(p), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")  # 64MB page cache
    return conn


@contextmanager
def get_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    """Create tables, indexes, and FTS5 virtual table if not present."""
    with get_db(path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(IDENTITY_SCHEMA_SQL)
        _migrate_schema(conn)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r["name"]) for r in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, ddl: str) -> None:
    col = ddl.split()[0]
    if col in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Lightweight additive migrations for existing local DBs."""
    _ensure_column(conn, "scrape_run", "incremental_enabled INTEGER DEFAULT 0")
    _ensure_column(conn, "scrape_run", "incremental_days INTEGER")
    _ensure_column(conn, "scrape_run", "preset_id INTEGER")
    _ensure_column(conn, "scrape_run", "total_inserted INTEGER DEFAULT 0")
    _ensure_column(conn, "scrape_run", "total_updated INTEGER DEFAULT 0")

    _ensure_column(conn, "scrape_run_ats", "rows_inserted INTEGER DEFAULT 0")
    _ensure_column(conn, "scrape_run_ats", "rows_updated INTEGER DEFAULT 0")
    _ensure_column(conn, "scrape_run_ats", "selected_companies INTEGER DEFAULT 0")
    _ensure_column(conn, "scrape_run_ats", "phase TEXT DEFAULT 'pending'")
    _ensure_column(conn, "scrape_run_ats", "phase_started_at TEXT")
    _ensure_column(conn, "scrape_run_ats", "eta_seconds INTEGER")
    _ensure_column(conn, "scrape_run_ats", "throughput_cpm REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_maintenance ("
        "key TEXT PRIMARY KEY, value TEXT, "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "role" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    sess_columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_sessions)").fetchall()}
    if "ip_address" not in sess_columns:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN ip_address TEXT")
    if "user_agent" not in sess_columns:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN user_agent TEXT")
    if "last_seen_at" not in sess_columns:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN last_seen_at TEXT")
    if "public_id" not in sess_columns:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN public_id TEXT")
        conn.execute("UPDATE auth_sessions SET public_id = lower(hex(randomblob(16))) WHERE public_id IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_sessions_public_id ON auth_sessions(public_id)")


def db_reclaimable_bytes(conn: sqlite3.Connection | None = None) -> int:
    """Bytes currently held by free pages — what VACUUM would return to disk."""
    if conn is not None:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        return page_size * free
    with get_db() as c:
        return db_reclaimable_bytes(c)


def last_vacuum_at() -> datetime | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM app_maintenance WHERE key='last_vacuum_at'"
        ).fetchone()
    if row is None or not row["value"]:
        return None
    try:
        return datetime.fromisoformat(str(row["value"])).astimezone(timezone.utc)
    except ValueError:
        return None


def vacuum_db(path: Path | None = None) -> dict:
    """Run VACUUM and record the timestamp. Returns before/after sizes."""
    p = Path(path) if path else settings.db_path
    size_before = p.stat().st_size if p.exists() else 0
    started = time.perf_counter()
    with get_db(path) as conn:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        free_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        free_after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO app_maintenance(key, value, updated_at) "
            "VALUES('last_vacuum_at', ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (now_iso,),
        )
    elapsed = time.perf_counter() - started
    size_after = p.stat().st_size if p.exists() else 0
    return {
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "reclaimed_bytes": max(0, size_before - size_after),
        "free_pages_before": free_before,
        "free_pages_after": free_after,
        "page_size": page_size,
        "duration_seconds": elapsed,
        "last_vacuum_at": now_iso,
    }


# Startup floor: don't pay the multi-minute VACUUM cost unless there's a
# meaningful amount to reclaim. Manual /api/settings/vacuum bypasses this.
STARTUP_VACUUM_MIN_BYTES = 100 * 1024 * 1024  # 100 MB


def vacuum_if_needed(min_reclaim_bytes: int = STARTUP_VACUUM_MIN_BYTES) -> dict | None:
    """Run VACUUM on startup iff cadence elapsed AND reclaimable >= threshold."""
    if not settings.db_vacuum_enabled:
        return None
    last = last_vacuum_at()
    min_hours = max(1, int(settings.db_vacuum_min_interval_hours))
    if last is not None:
        elapsed_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if elapsed_h < min_hours:
            return None
    reclaimable = db_reclaimable_bytes()
    if reclaimable < max(0, int(min_reclaim_bytes)):
        return None
    log.info(
        "startup VACUUM: ~%.1f MB reclaimable, running…",
        reclaimable / (1024 * 1024),
    )
    return vacuum_db()


# ---- jobs_total cache -----------------------------------------------------
# `SELECT COUNT(*) FROM jobs` is a full b-tree scan on a multi-GB DB and was
# blocking every dashboard render for 15-20s on a cold OS page cache. We keep
# a materialized total in app_maintenance (refreshed by ingestion) plus a
# process-local TTL so hot requests never touch the DB at all.

_JOBS_TOTAL_TTL_SECONDS = 60.0
_jobs_total_lock = threading.Lock()
_jobs_total_cache: tuple[int, float] | None = None  # (value, monotonic_expires_at)


def _read_jobs_total_marker(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM app_maintenance WHERE key='jobs_total'"
    ).fetchone()
    if row is None or row["value"] is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _write_jobs_total_marker(conn: sqlite3.Connection, total: int) -> None:
    conn.execute(
        "INSERT INTO app_maintenance(key, value, updated_at) "
        "VALUES('jobs_total', ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_at=datetime('now')",
        (str(int(total)),),
    )


def _store_jobs_total_cache(total: int) -> None:
    global _jobs_total_cache
    with _jobs_total_lock:
        _jobs_total_cache = (total, time.monotonic() + _JOBS_TOTAL_TTL_SECONDS)


def cached_jobs_total() -> int:
    """Total `jobs` row count, served from a process cache + DB marker.

    Falls back to a one-time COUNT(*) on first call if no marker has ever
    been written (e.g. a fresh checkout that hasn't ingested yet).
    """
    global _jobs_total_cache
    now = time.monotonic()
    with _jobs_total_lock:
        if _jobs_total_cache is not None and _jobs_total_cache[1] > now:
            return _jobs_total_cache[0]

    with get_db() as conn:
        total = _read_jobs_total_marker(conn)
        if total is None:
            total = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
            _write_jobs_total_marker(conn, total)
    _store_jobs_total_cache(total)
    return total


def refresh_jobs_total(conn: sqlite3.Connection) -> int:
    """Recompute COUNT(*), persist the marker, refresh the in-process cache."""
    total = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    _write_jobs_total_marker(conn, total)
    _store_jobs_total_cache(total)
    return total


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index from the jobs table.

    Run after large ingestion batches. The 'rebuild' command is FTS5's
    bulk reindex from the external content table.
    """
    conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")


def optimize_fts(conn: sqlite3.Connection) -> None:
    """Compact the FTS5 index. Cheap, idempotent."""
    conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('optimize')")


def import_identity_db(src_path: Path | None = None) -> dict[str, int]:
    """Idempotently import legacy identity-service tables into applyd DB."""
    src = Path(src_path) if src_path else settings.identity_legacy_db_path
    if not src.exists():
        raise FileNotFoundError(f"legacy identity DB not found: {src}")
    dst = settings.db_path
    backup_dir = dst.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dst, backup_dir / f"{dst.stem}.pre_identity_import.db")
    shutil.copy2(src, backup_dir / f"{src.stem}.source_identity.db")
    with get_db() as conn:
        conn.execute(f"ATTACH DATABASE '{src}' AS legacy_identity")
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO users (id, name, email, password_hash, role, created_at) "
                "SELECT id, name, lower(email), password_hash, COALESCE(role, 'user'), created_at "
                "FROM legacy_identity.users"
            )
            conn.execute(
                "INSERT OR IGNORE INTO auth_sessions (public_id, token, user_id, created_at, expires_at, ip_address, user_agent, last_seen_at) "
                "SELECT COALESCE(public_id, lower(hex(randomblob(16)))), token, user_id, created_at, expires_at, ip_address, user_agent, last_seen_at "
                "FROM legacy_identity.auth_sessions"
            )
            conn.execute(
                "INSERT OR REPLACE INTO auth_rate_limits (bucket_key, failed_attempts, window_started_at, locked_until) "
                "SELECT bucket_key, failed_attempts, window_started_at, locked_until "
                "FROM legacy_identity.auth_rate_limits"
            )
            conn.execute(
                "INSERT OR IGNORE INTO auth_events (id, event_type, email, user_id, ip_address, user_agent, success, detail, created_at) "
                "SELECT id, event_type, email, user_id, ip_address, user_agent, success, detail, created_at "
                "FROM legacy_identity.auth_events"
            )
            conn.execute(
                "INSERT OR REPLACE INTO auth_policy (key, value, updated_at, updated_by) "
                "SELECT key, value, updated_at, updated_by FROM legacy_identity.auth_policy"
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("DETACH DATABASE legacy_identity")
        return {
            "users": int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "sessions": int(conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0]),
            "events": int(conn.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0]),
            "rate_limits": int(conn.execute("SELECT COUNT(*) FROM auth_rate_limits").fetchone()[0]),
        }
