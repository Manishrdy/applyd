"""Unit tests for app.admin.audit — record() + list_recent()."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.admin import audit
from app.admin.deps import AdminUser
from app.database import get_db


@pytest.fixture()
def admin_user():
    return AdminUser(id=42, email="admin@test", role="admin")


@pytest.fixture()
def fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"user-agent": "pytest/1.0"},
    )


def test_record_writes_row(test_db_path, admin_user, fake_request):
    audit.record(
        admin=admin_user,
        action="terminate_session",
        target="pid-abc",
        detail={"reason": "test"},
        request=fake_request,
    )
    entries = audit.list_recent(limit=5)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.admin_user_id == 42
    assert entry.admin_email == "admin@test"
    assert entry.action == "terminate_session"
    assert entry.target == "pid-abc"
    assert entry.ip_address == "10.0.0.5"
    assert entry.user_agent == "pytest/1.0"
    # detail is JSON-serialised
    assert '"reason"' in entry.detail


def test_record_handles_string_detail(test_db_path, admin_user):
    audit.record(admin=admin_user, action="vacuum_db", detail="ran by hand")
    entries = audit.list_recent(limit=5)
    assert entries[0].detail == "ran by hand"


def test_record_handles_none_detail(test_db_path, admin_user):
    audit.record(admin=admin_user, action="enable_maintenance_mode")
    entries = audit.list_recent(limit=5)
    assert entries[0].detail is None


def test_record_never_raises_on_db_error(test_db_path, admin_user, monkeypatch):
    """Audit is best-effort — a DB outage must not crash the action it logs."""

    def boom(*a, **kw):
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(audit, "get_db", boom)
    # Must not raise.
    audit.record(admin=admin_user, action="dangerous_op", target="x")


def test_list_recent_filters_by_action(test_db_path, admin_user):
    audit.record(admin=admin_user, action="terminate_session", target="a")
    audit.record(admin=admin_user, action="clear_failed_logins", target="b")
    audit.record(admin=admin_user, action="terminate_session", target="c")

    entries = audit.list_recent(action="terminate_session", limit=10)
    assert {e.target for e in entries} == {"a", "c"}


def test_list_recent_orders_newest_first(test_db_path, admin_user):
    audit.record(admin=admin_user, action="first", target="1")
    audit.record(admin=admin_user, action="second", target="2")
    audit.record(admin=admin_user, action="third", target="3")
    entries = audit.list_recent(limit=10)
    assert [e.action for e in entries] == ["third", "second", "first"]


def test_list_recent_caps_limit(test_db_path, admin_user):
    for i in range(10):
        audit.record(admin=admin_user, action="x", target=str(i))
    # Anything >500 should be clamped; smaller values respected.
    assert len(audit.list_recent(limit=3)) == 3
    assert len(audit.list_recent(limit=9999)) == 10
