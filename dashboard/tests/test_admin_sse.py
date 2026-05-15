"""Tests for the admin SSE stream endpoint.

We tune the module-level cadence constants down to milliseconds so the
tests run in the same time budget as the rest of the suite.
"""

from __future__ import annotations

import json
import time

import pytest

from app.admin.routers import system as system_router


@pytest.fixture()
def fast_stream(monkeypatch):
    """Shorten cadence so the stream produces a few frames in <1 second."""
    monkeypatch.setattr(system_router, "STREAM_MAX_SECONDS", 1.0)
    monkeypatch.setattr(system_router, "STREAM_DATA_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(system_router, "STREAM_HEARTBEAT_SECONDS", 0.05)


# ---- gating ---------------------------------------------------------------


def test_stream_requires_admin(client):
    res = client.get("/api/admin/stream/health")
    assert res.status_code == 403


def test_stream_unauthenticated_blocked(anon_client):
    res = anon_client.get("/api/admin/stream/health")
    # Auth middleware fires before the route — 401 JSON for /api/* anon.
    assert res.status_code == 401


# ---- shape ---------------------------------------------------------------


def test_stream_content_type_and_initial_event(admin_client, fast_stream):
    with admin_client.stream("GET", "/api/admin/stream/health") as res:
        assert res.status_code == 200
        ct = res.headers["content-type"]
        assert ct.startswith("text/event-stream")
        assert res.headers.get("cache-control") == "no-store"
        assert res.headers.get("x-accel-buffering") == "no"

        # Read enough bytes to see the hello frame + at least one data frame.
        chunks: list[str] = []
        deadline = time.monotonic() + 2.0
        for chunk in res.iter_text():
            chunks.append(chunk)
            if "hello" in "".join(chunks) and "\"db\"" in "".join(chunks):
                break
            if time.monotonic() > deadline:
                pytest.fail("did not receive expected frames in time: " + "".join(chunks))

    body = "".join(chunks)
    assert "event: hello" in body
    # The data frame should embed a JSON snapshot with the keys we expect.
    # SSE delimits frames with a blank line; pick the first data line.
    data_lines = [ln[len("data: "):] for ln in body.splitlines() if ln.startswith("data: ")]
    assert data_lines, "no data: lines emitted"
    # The very first data: line is the hello payload, the next is a snapshot.
    snapshot = json.loads(data_lines[1])
    assert {"db", "ingestion", "scrape", "cache", "maintenance", "now"} <= snapshot.keys()


def test_stream_emits_timeout_event_and_closes(admin_client, fast_stream):
    """With STREAM_MAX_SECONDS=1 the server should signal timeout and close."""
    with admin_client.stream("GET", "/api/admin/stream/health") as res:
        assert res.status_code == 200
        body = ""
        deadline = time.monotonic() + 3.0
        for chunk in res.iter_text():
            body += chunk
            if "event: timeout" in body:
                break
            if time.monotonic() > deadline:
                pytest.fail("never observed timeout event: " + body[-200:])

    assert "event: timeout" in body
    # Heartbeat comments shouldn't dominate — at least one real data frame landed.
    assert body.count("data: ") >= 2


def test_stream_skips_bad_tick_without_crashing(admin_client, monkeypatch):
    """If build_snapshot raises mid-stream the loop logs and keeps going."""
    monkeypatch.setattr(system_router, "STREAM_MAX_SECONDS", 0.6)
    monkeypatch.setattr(system_router, "STREAM_DATA_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(system_router, "STREAM_HEARTBEAT_SECONDS", 0.05)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated snapshot failure")
        return {"db": {}, "ingestion": {}, "cache": {}, "scrape": {}, "maintenance": {}, "now": "t"}

    monkeypatch.setattr(system_router, "build_snapshot", flaky)

    with admin_client.stream("GET", "/api/admin/stream/health") as res:
        assert res.status_code == 200
        body = ""
        deadline = time.monotonic() + 3.0
        for chunk in res.iter_text():
            body += chunk
            if "event: timeout" in body:
                break
            if time.monotonic() > deadline:
                pytest.fail("never observed timeout event: " + body[-200:])

    # Bad tick logged + skipped, stream still closed cleanly via timeout.
    assert calls["n"] >= 2
    assert "event: timeout" in body
