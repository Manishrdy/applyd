"""SQLite database setup: schema, FTS5, WAL, connection helper."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings


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
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else settings.db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
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


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 index from the jobs table.

    Run after large ingestion batches. The 'rebuild' command is FTS5's
    bulk reindex from the external content table.
    """
    conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('rebuild')")


def optimize_fts(conn: sqlite3.Connection) -> None:
    """Compact the FTS5 index. Cheap, idempotent."""
    conn.execute("INSERT INTO jobs_fts(jobs_fts) VALUES('optimize')")
