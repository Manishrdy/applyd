"""Manifest fetch + freshness diff against manifest_log."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)


async def fetch_manifest() -> dict[str, Any]:
    """Fetch and parse the jobhive manifest.json from upstream."""
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        r = await client.get(settings.manifest_url)
        r.raise_for_status()
        return r.json()


def latest_manifest_log(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM manifest_log "
        "WHERE status = 'success' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row


def should_ingest(conn: sqlite3.Connection, manifest: dict[str, Any]) -> bool:
    """True if the manifest has a newer updated_at than our last successful ingest."""
    last = latest_manifest_log(conn)
    if last is None:
        return True
    upstream_updated_at = manifest.get("updated_at")
    if not upstream_updated_at:
        log.warning("manifest has no 'updated_at' field; ingesting anyway")
        return True
    return upstream_updated_at != last["manifest_updated_at"]


def list_ats(manifest: dict[str, Any]) -> list[str]:
    """Return all ATS names available in the manifest's by_ats section."""
    by_ats = manifest.get("by_ats") or {}
    return sorted(by_ats.keys())


def ats_meta(manifest: dict[str, Any], ats: str) -> dict[str, Any] | None:
    """Return the by_ats[<ats>] entry (parquet URL, sha256, rows, etc.)."""
    return (manifest.get("by_ats") or {}).get(ats)
