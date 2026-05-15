"""Tests for the maintenance service and middleware behaviour."""

from __future__ import annotations

from app.admin.services import maintenance as maintenance_service


# ---- service ---------------------------------------------------------------


def test_status_defaults_to_disabled(test_db_path):
    s = maintenance_service.get_status()
    assert s.enabled is False
    assert s.message == ""
    assert s.enabled_by is None


def test_enable_persists_message_and_caller(test_db_path):
    s = maintenance_service.enable("Be right back.", enabled_by="admin@test")
    assert s.enabled is True
    assert s.message == "Be right back."
    assert s.enabled_by == "admin@test"

    # Re-read independently — make sure it survived the write.
    again = maintenance_service.get_status()
    assert again.enabled is True
    assert again.enabled_by == "admin@test"


def test_disable_keeps_message_for_audit(test_db_path):
    maintenance_service.enable("Down for repairs", enabled_by="admin@test")
    s = maintenance_service.disable(enabled_by="admin@test")
    assert s.enabled is False
    assert s.message == "Down for repairs"


# ---- middleware ------------------------------------------------------------


def test_middleware_lets_non_admin_through_when_off(client):
    """Default state: maintenance off → ordinary user can still hit /api/saved/."""
    res = client.get("/api/saved/")
    assert res.status_code == 200


def test_middleware_blocks_non_admin_when_on(client):
    maintenance_service.enable("Down briefly", enabled_by="admin@test")
    try:
        res = client.get("/api/saved/")
        assert res.status_code == 503
        body = res.json()
        assert body["detail"] == "service under maintenance"
    finally:
        maintenance_service.disable(enabled_by="admin@test")


def test_middleware_lets_admin_through_when_on(admin_client):
    maintenance_service.enable("Down briefly", enabled_by="admin@test")
    try:
        res = admin_client.get("/api/saved/")
        # Admin bypasses the 503; saved/ might be empty but it's reachable.
        assert res.status_code == 200
    finally:
        maintenance_service.disable(enabled_by="admin@test")


def test_middleware_exempts_admin_paths_from_block(client):
    """Even when on, admin URLs stay reachable so an admin can flip it back."""
    maintenance_service.enable("Down briefly", enabled_by="admin@test")
    try:
        # Non-admin hitting /api/admin/* — auth_middleware still 403s them
        # because they aren't admin, but the maintenance gate is not what
        # rejects them. Either status proves maintenance didn't blanket-503.
        res = client.get("/api/admin/health")
        assert res.status_code != 503
    finally:
        maintenance_service.disable(enabled_by="admin@test")


def test_middleware_exempts_health(client):
    maintenance_service.enable("Down briefly", enabled_by="admin@test")
    try:
        res = client.get("/api/health")
        assert res.status_code == 200
    finally:
        maintenance_service.disable(enabled_by="admin@test")
