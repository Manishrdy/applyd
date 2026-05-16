"""Verifier service — matcher correctness and circuit-breaker semantics."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.database import get_db
from app.services import verifier


def _resp(status: int, body: str, url: str = "https://jobs.example/1") -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = body
    r.url = url
    return r


def test_match_greenhouse_404():
    assert verifier.match_greenhouse("", 404) == "expired"


def test_match_greenhouse_body():
    body = "<html>this job is no longer available</html>".lower()
    assert verifier.match_greenhouse(body, 200) == "expired"


def test_match_ashby_body():
    body = "this job is no longer available."
    assert verifier.match_ashby(body, 200) == "expired"


def test_match_workday_does_not_404():
    body = "this job posting is no longer available"
    assert verifier.match_workday(body, 200) == "expired"


def test_match_generic_status_only():
    assert verifier.match_generic("any noisy body text", 200) is None
    assert verifier.match_generic("", 404) == "expired"


def test_match_lever_active_marker():
    body = "<div id=lever-application></div>"
    assert verifier.match_lever(body, 200) == "active"


def test_verify_job_404_returns_expired():
    client = MagicMock()
    client.head = AsyncMock(return_value=_resp(404, ""))
    res = asyncio.run(verifier.verify_job(client, job_id=1, url="x", ats_type="greenhouse"))
    assert res.result == "expired"
    assert res.http_status == 404


def test_verify_job_200_active_default():
    client = MagicMock()
    client.head = AsyncMock(return_value=_resp(200, ""))
    client.get = AsyncMock(return_value=_resp(200, "<html>jobs page</html>".lower()))
    res = asyncio.run(verifier.verify_job(client, job_id=1, url="x", ats_type="unknown_ats"))
    assert res.result == "active"


def test_verify_job_listing_redirect_is_expired():
    client = MagicMock()
    client.head = AsyncMock(return_value=_resp(302, ""))
    client.get = AsyncMock(return_value=_resp(200, "", url="https://boards.greenhouse.io/acme/jobs"))
    res = asyncio.run(verifier.verify_job(client, job_id=1, url="x", ats_type="greenhouse"))
    assert res.result == "expired"


def test_verify_job_429_backs_off(monkeypatch):
    client = MagicMock()
    client.head = AsyncMock(return_value=_resp(429, ""))
    res = asyncio.run(verifier.verify_job(client, job_id=1, url="x", ats_type="ashby"))
    assert res.result == "unknown"
    assert "backoff" in (res.detail or "")


def test_verify_job_timeout_returns_error():
    client = MagicMock()
    client.head = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    res = asyncio.run(verifier.verify_job(client, job_id=1, url="x", ats_type="greenhouse"))
    assert res.result == "error"


def test_circuit_breaker_trips_after_threshold(test_db_path, monkeypatch):
    """Repeated expirations from one ATS in one hour trip the breaker."""
    from app.services import job_lifecycle
    monkeypatch.setattr(settings, "verifier_auto_marking_enabled", True)
    monkeypatch.setattr(settings, "verifier_circuit_breaker_threshold", 2)
    with get_db(test_db_path) as conn:
        # Three 404s should trip the breaker; the third must stay non-expired.
        for jid in (1, 2):
            job_lifecycle.on_http_check(
                conn, jid, result="expired", http_status=404,
                detector="match_greenhouse", detail="HTTP 404",
            )
        # 3rd one: breaker should trip and the row stays active.
        # All three test rows are ats_type 'greenhouse' is not true; row 2 is
        # 'lever'. We pick same-ats rows for the trip test.
        # Insert two more greenhouse jobs for a controlled run.
        for new_id in (901, 902, 903):
            conn.execute(
                "INSERT INTO jobs (id, url, ats_type, verification_status) "
                "VALUES (?, ?, 'greenhouse', 'active')",
                (new_id, f"https://jobs.example/{new_id}"),
            )
        for jid in (901, 902):
            job_lifecycle.on_http_check(
                conn, jid, result="expired", http_status=404,
                detector="match_greenhouse", detail="HTTP 404",
            )
        # Third one should be blocked by the breaker.
        job_lifecycle.on_http_check(
            conn, 903, result="expired", http_status=404,
            detector="match_greenhouse", detail="HTTP 404",
        )
        status = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = 903"
        ).fetchone()["verification_status"]
        assert status == "active"
