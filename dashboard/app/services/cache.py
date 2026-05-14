"""Lightweight Redis cache helper with fail-open semantics.

If Redis is not configured or unavailable, all operations no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

_client = None
_cache_disabled = False
_VERSION_KEY = "jobs:cache:version"


def _get_client():
    global _client, _cache_disabled
    if not settings.redis_cache_enabled:
        _cache_disabled = True
        return None
    if _cache_disabled:
        return None
    if _client is not None:
        return _client
    if not settings.redis_url:
        _cache_disabled = True
        return None
    try:
        import redis

        _client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=0.2,
            socket_connect_timeout=0.2,
        )
        _client.ping()
        return _client
    except Exception:
        log.warning("redis cache unavailable; continuing without cache", exc_info=True)
        _cache_disabled = True
        return None


def jobs_cache_version() -> str:
    c = _get_client()
    if c is None:
        return "0"
    try:
        v = c.get(_VERSION_KEY)
        return str(v or "0")
    except Exception:
        return "0"


def bump_jobs_cache_version() -> None:
    c = _get_client()
    if c is None:
        return
    try:
        c.incr(_VERSION_KEY)
    except Exception:
        log.debug("could not bump jobs cache version", exc_info=True)


def make_jobs_key(payload: dict[str, Any], *, version: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"jobs:search:v{version}:{digest}"


def get_json(key: str) -> str | None:
    c = _get_client()
    if c is None:
        return None
    try:
        val = c.get(key)
        return str(val) if val is not None else None
    except Exception:
        return None


def set_json(key: str, value: str, *, ttl_seconds: int) -> None:
    c = _get_client()
    if c is None:
        return
    try:
        c.setex(key, max(1, int(ttl_seconds)), value)
    except Exception:
        log.debug("cache set failed for key=%s", key, exc_info=True)
