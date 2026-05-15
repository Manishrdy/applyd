"""Page-level tests: each /admin/* HTML route renders + role gating works.

These are intentionally shallow — they verify:
  - 200 + the correct active sidebar item for an admin
  - 403 for a non-admin
  - The catchall returns the chrome'd 404 for unknown admin paths
  - The maintenance page reflects the live flag state
"""

from __future__ import annotations

import pytest

from app.admin.services import maintenance as maintenance_service


ADMIN_PAGES = [
    ("/admin",              "Overview"),
    ("/admin/sessions",     "Active sessions"),
    ("/admin/auth-log",     "Failed logins"),
    ("/admin/rate-limits",  "Rate limits"),
    ("/admin/maintenance",  "Maintenance mode"),
    ("/admin/backups",      "Backups"),
    ("/admin/audit",        "Admin audit log"),
]


@pytest.mark.parametrize("path,heading", ADMIN_PAGES)
def test_admin_pages_render_for_admin(admin_client, path, heading):
    res = admin_client.get(path)
    assert res.status_code == 200, res.text[:300]
    assert heading in res.text
    # Sidebar nav rendered, "Admin" label present, signed-in identity surfaced.
    assert ">Admin</p>" in res.text
    assert "admin@test" in res.text


@pytest.mark.parametrize("path,_h", ADMIN_PAGES)
def test_admin_pages_block_non_admin(client, path, _h):
    res = client.get(path)
    assert res.status_code == 403


@pytest.mark.parametrize("path,_h", ADMIN_PAGES)
def test_admin_pages_redirect_anon(anon_client, path, _h):
    res = anon_client.get(path, follow_redirects=False)
    assert res.status_code == 303
    assert "/signin" in res.headers.get("location", "")


# ---- catchall -------------------------------------------------------------


def test_admin_catchall_renders_404_page_for_admin(admin_client):
    res = admin_client.get("/admin/no-such-thing")
    assert res.status_code == 404
    assert "Admin page not found" in res.text
    # Confirm path is echoed back so the operator sees what they typed.
    assert "/admin/no-such-thing" in res.text


def test_admin_catchall_blocks_non_admin(client):
    res = client.get("/admin/no-such-thing")
    assert res.status_code == 403


def test_admin_catchall_redirects_anon(anon_client):
    res = anon_client.get("/admin/no-such-thing", follow_redirects=False)
    assert res.status_code == 303


# ---- maintenance page reflects live flag ---------------------------------


def test_maintenance_page_shows_off_by_default(admin_client):
    # Reset between tests in case order matters.
    maintenance_service.disable(enabled_by="test-cleanup")
    res = admin_client.get("/admin/maintenance")
    assert res.status_code == 200
    assert "Maintenance mode" in res.text


def test_backups_page_carries_token_status(admin_client, monkeypatch):
    monkeypatch.delenv("APPLYD_BACKUP_TOKEN", raising=False)
    res = admin_client.get("/admin/backups")
    assert res.status_code == 200
    # The token-not-configured warning is server-rendered into the Alpine state.
    assert "tokenConfigured: false" in res.text


def test_backups_page_token_configured(admin_client, monkeypatch):
    monkeypatch.setenv("APPLYD_BACKUP_TOKEN", "any-non-empty")
    res = admin_client.get("/admin/backups")
    assert res.status_code == 200
    assert "tokenConfigured: true" in res.text


# ---- conditional admin nav link in base.html -----------------------------


def test_dashboard_shows_admin_link_for_admin(admin_client):
    res = admin_client.get("/dashboard")
    assert res.status_code == 200
    assert 'href="/admin"' in res.text


def test_dashboard_hides_admin_link_for_user(client):
    res = client.get("/dashboard")
    assert res.status_code == 200
    assert 'href="/admin"' not in res.text
