from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings


SCHEMA_SQL = """
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
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else settings.db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with get_db(path) as conn:
        conn.executescript(SCHEMA_SQL)

        # SQLite can't add a CHECK constraint via ALTER, so existing tables
        # get the column without it; new installs get it from CREATE TABLE.
        # Python-side validation in create_user() keeps bad values out.
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(auth_sessions)").fetchall()}
        if "ip_address" not in columns:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN ip_address TEXT")
        if "user_agent" not in columns:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN user_agent TEXT")
        if "last_seen_at" not in columns:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN last_seen_at TEXT")
        if "public_id" not in columns:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN public_id TEXT")
            rows = conn.execute(
                "SELECT token FROM auth_sessions WHERE public_id IS NULL OR TRIM(COALESCE(public_id, '')) = ''",
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE auth_sessions SET public_id = ? WHERE token = ?",
                    (secrets.token_urlsafe(16), row["token"]),
                )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_sessions_public_id ON auth_sessions(public_id)")

        # Migrate to sha256(token)-at-rest. Any session token that doesn't
        # match the 64-char lowercase-hex shape of sha256 was written by the
        # pre-hardening schema and is unsafe to keep. Wipe — the affected
        # users will be prompted to sign in again. This is a one-time hit.
        conn.execute(
            "DELETE FROM auth_sessions "
            "WHERE token IS NULL "
            "OR length(token) != 64 "
            "OR token GLOB '*[^0-9a-f]*'"
        )
