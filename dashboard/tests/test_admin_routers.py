"""Integration tests for admin JSON routers.

These exercise the auth gating + audit logging without depending on the
remote identity-service: the identity HTTP client is stubbed at the
module boundary so router behaviour can be verified in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin import audit as audit_service


# ---- shared stub helpers --------------------------------------------------


class _StubIdentity:
    """Replaces app.admin.services.identity for router-level tests."""

    def __init__(self):
        self.calls = []

    async def list_sessions(self, request):
        self.calls.append(("list_sessions",))
        return [{"public_id": "abc", "user_id": 1, "email": "u@test"}]

    async def terminate_session(self, request, public_id, csrf_token):
        self.calls.append(("terminate_session", public_id, csrf_token))
        return {"terminated": True, "public_id": public_id}

    async def list_failed_logins(self, request, *, limit=100):
        self.calls.append(("list_failed_logins", limit))
        return [{"id": 1, "event_type": "signin", "email": "u@test"}]

    async def clear_failed_logins(self, request, csrf_token, *, email=None, ip_address=None):
        self.calls.append(("clear_failed_logins", csrf_token, email, ip_address))
        return {"cleared": 1, "events_deleted": 2}

    async def list_rate_limits(self, request):
        self.calls.append(("list_rate_limits",))
        return {"policy": {"pair_max": 5}, "locked_buckets": []}

    async def update_rate_limit_policy(self, request, csrf_token, **kwargs):
        self.calls.append(("update_rate_limit_policy", csrf_token, kwargs))
        return {"policy": kwargs}

    async def unlock_rate_limit_bucket(self, request, csrf_token, bucket_key):
        self.calls.append(("unlock", csrf_token, bucket_key))
        return {"unlocked": True, "bucket_key": bucket_key}


@pytest.fixture()
def stub_identity(monkeypatch):
    stub = _StubIdentity()
    # Patch each router's binding of the identity_client module. Each router
    # does `from app.admin.services import identity as identity_client`, so
    # patching the attribute on each router catches the call.
    from app.admin.routers import sessions as sessions_router
    from app.admin.routers import failed_logins as failed_logins_router
    from app.admin.routers import rate_limits as rate_limits_router

    monkeypatch.setattr(sessions_router, "identity_client", stub)
    monkeypatch.setattr(failed_logins_router, "identity_client", stub)
    monkeypatch.setattr(rate_limits_router, "identity_client", stub)
    return stub


# ---- gating ---------------------------------------------------------------


def test_user_cannot_hit_admin_endpoints(client, stub_identity):
    res = client.get("/api/admin/sessions")
    assert res.status_code == 403


def test_anon_redirected_for_admin_endpoints(anon_client, stub_identity):
    # No session cookie → 401 JSON for /api/admin/*.
    res = anon_client.get("/api/admin/sessions")
    assert res.status_code == 401


# ---- sessions -------------------------------------------------------------


def test_sessions_list_proxies_identity(admin_client, stub_identity):
    res = admin_client.get("/api/admin/sessions")
    assert res.status_code == 200
    assert res.json() == [{"public_id": "abc", "user_id": 1, "email": "u@test"}]
    assert stub_identity.calls[-1][0] == "list_sessions"


def test_sessions_terminate_records_audit(admin_client, stub_identity):
    res = admin_client.post(
        "/api/admin/sessions/abc/terminate",
        data={"csrf_token": "test-csrf"},
    )
    assert res.status_code == 200
    # Audit row written.
    entries = audit_service.list_recent(limit=10, action="terminate_session")
    assert any(e.target == "abc" for e in entries)


# ---- failed logins --------------------------------------------------------


def test_failed_logins_list(admin_client, stub_identity):
    res = admin_client.get("/api/admin/failed-logins?limit=25")
    assert res.status_code == 200
    assert stub_identity.calls[-1] == ("list_failed_logins", 25)


def test_failed_logins_clear_records_audit(admin_client, stub_identity):
    res = admin_client.post(
        "/api/admin/failed-logins/clear",
        data={"csrf_token": "test-csrf", "email": "victim@test"},
    )
    assert res.status_code == 200
    entries = audit_service.list_recent(limit=10, action="clear_failed_logins")
    assert any(e.target == "victim@test" for e in entries)


# ---- rate limits ----------------------------------------------------------


def test_rate_limits_policy_update_records_audit(admin_client, stub_identity):
    res = admin_client.post(
        "/api/admin/rate-limits/policy",
        data={
            "csrf_token": "test-csrf",
            "pair_max": 4,
            "email_max": 10,
            "ip_max": 20,
            "window_seconds": 120,
            "lockout_seconds": 300,
        },
    )
    assert res.status_code == 200
    entries = audit_service.list_recent(limit=10, action="update_rate_limit_policy")
    assert len(entries) >= 1


def test_rate_limits_unlock_records_audit(admin_client, stub_identity):
    res = admin_client.post(
        "/api/admin/rate-limits/unlock",
        data={"csrf_token": "test-csrf", "bucket_key": "email::someone@test"},
    )
    assert res.status_code == 200
    entries = audit_service.list_recent(limit=10, action="unlock_rate_limit_bucket")
    assert any(e.target == "email::someone@test" for e in entries)


# ---- system + audit endpoints --------------------------------------------


def test_admin_health(admin_client):
    res = admin_client.get("/api/admin/health")
    assert res.status_code == 200
    body = res.json()
    assert "db" in body and "ingestion" in body and "maintenance" in body


def test_admin_audit_list(admin_client):
    audit_service.record(
        admin=__import__("app.admin.deps", fromlist=["AdminUser"]).AdminUser(
            id=42, email="admin@test", role="admin"
        ),
        action="seeded_for_test",
        target="x",
    )
    res = admin_client.get("/api/admin/audit?action=seeded_for_test")
    assert res.status_code == 200
    rows = res.json()
    assert any(r["action"] == "seeded_for_test" for r in rows)


# ---- maintenance router ---------------------------------------------------


def test_maintenance_enable_disable_round_trip(admin_client):
    enable = admin_client.post("/api/admin/maintenance/enable", data={"message": "brief outage"})
    assert enable.status_code == 200
    assert enable.json()["enabled"] is True
    assert enable.json()["message"] == "brief outage"

    disable = admin_client.post("/api/admin/maintenance/disable")
    assert disable.status_code == 200
    assert disable.json()["enabled"] is False


# ---- catchall -------------------------------------------------------------


def test_api_catchall_returns_json_404(admin_client):
    res = admin_client.get("/api/admin/something-that-does-not-exist")
    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/json")
    assert "unknown admin endpoint" in res.json()["detail"]


def test_api_catchall_blocks_non_admin(client):
    res = client.get("/api/admin/something-else")
    assert res.status_code == 403
