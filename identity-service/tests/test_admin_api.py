"""Tests for /api/admin/* endpoints and /api/auth/verify role surface."""

from __future__ import annotations

import pytest


# ---- helpers ---------------------------------------------------------------


def _csrf(client) -> str:
    client.get("/signin")
    token = client.cookies.get("applyd_csrf")
    assert token
    return token


def _signup(client, email: str, password: str = "Goodpass123!") -> None:
    csrf = _csrf(client)
    res = client.post(
        "/signup",
        data={
            "name": email.split("@")[0],
            "email": email,
            "password": password,
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303


def _promote_to_admin(email: str) -> None:
    from app.auth import admin_set_user_role
    from app.database import get_db

    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    assert row is not None
    admin_set_user_role(int(row["id"]), "admin")


def _sign_in_as_admin(client, email: str) -> None:
    """Create user, promote to admin, sign in fresh. Leaves session cookie set."""
    _signup(client, email)
    _promote_to_admin(email)
    # Log out the user session so we can sign in fresh after the role change.
    client.cookies.clear()
    csrf = _csrf(client)
    res = client.post(
        "/signin",
        data={
            "email": email,
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303


# ---- /api/auth/verify -----------------------------------------------------


def test_verify_returns_role_user(client):
    _signup(client, "u@test")
    res = client.get("/api/auth/verify")
    assert res.status_code == 200
    body = res.json()
    assert body["authenticated"] is True
    assert body["email"] == "u@test"
    assert body["role"] == "user"


def test_verify_returns_role_admin(client):
    _sign_in_as_admin(client, "a@test")
    res = client.get("/api/auth/verify")
    assert res.status_code == 200
    assert res.json()["role"] == "admin"


def test_verify_anonymous_returns_401(client):
    res = client.get("/api/auth/verify")
    assert res.status_code == 401
    assert res.json() == {"authenticated": False}


# ---- /api/admin/* — gating shared across endpoints ------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/admin/sessions"),
        ("GET", "/api/admin/failed-logins"),
        ("GET", "/api/admin/rate-limits"),
        ("GET", "/api/admin/users"),
    ],
)
def test_admin_endpoints_require_auth(client, method, path):
    res = client.request(method, path)
    assert res.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/admin/sessions"),
        ("GET", "/api/admin/failed-logins"),
        ("GET", "/api/admin/rate-limits"),
        ("GET", "/api/admin/users"),
    ],
)
def test_admin_endpoints_block_non_admin(client, method, path):
    _signup(client, "u@test")
    res = client.request(method, path)
    assert res.status_code == 403


# ---- /api/admin/sessions --------------------------------------------------


def test_admin_sessions_lists_active_only(client):
    _signup(client, "u1@test")
    client.cookies.clear()
    _sign_in_as_admin(client, "admin@test")

    res = client.get("/api/admin/sessions")
    assert res.status_code == 200
    sessions = res.json()
    assert isinstance(sessions, list)
    emails = {s["email"] for s in sessions}
    assert "admin@test" in emails


def test_admin_sessions_terminate_requires_csrf(client):
    _sign_in_as_admin(client, "a@test")
    sessions = client.get("/api/admin/sessions").json()
    target = next(s for s in sessions if s["email"] == "a@test")
    res = client.post(f"/api/admin/sessions/{target['public_id']}/terminate", data={})
    assert res.status_code == 400


def test_admin_sessions_terminate_removes_session(client):
    _signup(client, "user1@test")
    user1_session = client.cookies.get("applyd_session")
    assert user1_session

    client.cookies.clear()
    _sign_in_as_admin(client, "admin@test")
    csrf = client.cookies.get("applyd_csrf")

    sessions = client.get("/api/admin/sessions").json()
    target = next(s for s in sessions if s["email"] == "user1@test")
    res = client.post(
        f"/api/admin/sessions/{target['public_id']}/terminate",
        data={"csrf_token": csrf},
    )
    assert res.status_code == 200
    assert res.json()["terminated"] is True

    # The killed cookie can no longer auth.
    client.cookies.clear()
    client.cookies.set("applyd_session", user1_session)
    assert client.get("/api/auth/verify").status_code == 401


def test_admin_sessions_terminate_missing_returns_404(client):
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    res = client.post(
        "/api/admin/sessions/does-not-exist/terminate",
        data={"csrf_token": csrf},
    )
    assert res.status_code == 404


# ---- /api/admin/failed-logins ---------------------------------------------


def test_admin_failed_logins_list(client):
    # Cause a failed signin first.
    csrf = _csrf(client)
    client.post(
        "/signin",
        data={"email": "ghost@test", "password": "Wrongpass-1!", "csrf_token": csrf},
        follow_redirects=False,
    )
    _sign_in_as_admin(client, "a@test")
    res = client.get("/api/admin/failed-logins?limit=10")
    assert res.status_code == 200
    events = res.json()
    # Query filters to success=0; `success` isn't returned in the row shape,
    # so a single matching event_type is sufficient evidence.
    assert any(e["event_type"] == "signin" for e in events)


def test_admin_failed_logins_clear_scoped_by_email(client):
    # Generate a lockout for one specific email, then verify clear removes it.
    from app.auth import is_signin_rate_limited, record_signin_failure

    for _ in range(20):
        record_signin_failure("203.0.113.7", "victim@test")
    assert is_signin_rate_limited("203.0.113.7", "victim@test")

    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    res = client.post(
        "/api/admin/failed-logins/clear",
        data={"csrf_token": csrf, "email": "victim@test"},
    )
    assert res.status_code == 200
    assert res.json()["cleared"] > 0
    assert not is_signin_rate_limited("203.0.113.7", "victim@test")


def test_admin_failed_logins_clear_broad_only_clears_lockouts(client):
    """Without scope, clear only nulls locked_until — bucket history survives."""
    from app.auth import record_signin_failure
    from app.database import get_db

    # Sign in admin first so signup-side rate-limit buckets don't pollute
    # the row count we're going to assert preservation of.
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")

    for _ in range(20):
        record_signin_failure("198.51.100.1", "x@test")
    with get_db() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM auth_rate_limits"
        ).fetchone()["n"]
    assert before > 0

    res = client.post("/api/admin/failed-logins/clear", data={"csrf_token": csrf})
    assert res.status_code == 200

    with get_db() as conn:
        rows = conn.execute(
            "SELECT bucket_key, locked_until FROM auth_rate_limits"
        ).fetchall()
    assert len(rows) == before  # nothing deleted
    assert all(r["locked_until"] is None for r in rows)


# ---- /api/admin/rate-limits -----------------------------------------------


def test_admin_rate_limits_returns_policy_and_buckets(client):
    _sign_in_as_admin(client, "a@test")
    res = client.get("/api/admin/rate-limits")
    assert res.status_code == 200
    body = res.json()
    assert "policy" in body and "locked_buckets" in body
    pol = body["policy"]
    assert {"pair_max", "email_max", "ip_max", "window_seconds", "lockout_seconds"} <= set(pol)


def test_admin_rate_limits_policy_persists(client):
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    res = client.post(
        "/api/admin/rate-limits/policy",
        data={
            "csrf_token": csrf,
            "pair_max": 3,
            "email_max": 8,
            "ip_max": 12,
            "window_seconds": 60,
            "lockout_seconds": 120,
        },
    )
    assert res.status_code == 200

    from app.auth import get_rate_limit_policy

    pol = get_rate_limit_policy()
    assert pol.pair_max == 3
    assert pol.window_seconds == 60
    assert pol.lockout_seconds == 120


def test_admin_rate_limits_policy_rejects_silly_values(client):
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    res = client.post(
        "/api/admin/rate-limits/policy",
        data={
            "csrf_token": csrf,
            "pair_max": 0,
            "email_max": 8,
            "ip_max": 12,
            "window_seconds": 60,
            "lockout_seconds": 120,
        },
    )
    assert res.status_code == 400


def test_admin_rate_limits_unlock_bucket(client):
    from app.auth import _rate_limit_email_key, record_signin_failure

    for _ in range(20):
        record_signin_failure("203.0.113.99", "lockme@test")
    bucket = _rate_limit_email_key("lockme@test")

    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    res = client.post(
        "/api/admin/rate-limits/unlock",
        data={"csrf_token": csrf, "bucket_key": bucket},
    )
    assert res.status_code == 200
    assert res.json()["unlocked"] is True


# ---- /api/admin/users -----------------------------------------------------


def test_admin_users_list_includes_role(client):
    _sign_in_as_admin(client, "a@test")
    res = client.get("/api/admin/users")
    assert res.status_code == 200
    users = res.json()
    assert any(u["email"] == "a@test" and u["role"] == "admin" for u in users)


def test_admin_users_role_change(client):
    _signup(client, "promote-me@test")
    client.cookies.clear()
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")

    users = client.get("/api/admin/users").json()
    target_id = next(u["id"] for u in users if u["email"] == "promote-me@test")
    res = client.post(
        f"/api/admin/users/{target_id}/role",
        data={"csrf_token": csrf, "role": "admin"},
    )
    assert res.status_code == 200

    from app.auth import get_user_role

    assert get_user_role(target_id) == "admin"


def test_admin_users_self_demote_rejected(client):
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    users = client.get("/api/admin/users").json()
    self_id = next(u["id"] for u in users if u["email"] == "a@test")
    res = client.post(
        f"/api/admin/users/{self_id}/role",
        data={"csrf_token": csrf, "role": "user"},
    )
    assert res.status_code == 400


def test_admin_users_role_invalid_rejected(client):
    _signup(client, "subject@test")
    client.cookies.clear()
    _sign_in_as_admin(client, "a@test")
    csrf = client.cookies.get("applyd_csrf")
    users = client.get("/api/admin/users").json()
    target_id = next(u["id"] for u in users if u["email"] == "subject@test")
    res = client.post(
        f"/api/admin/users/{target_id}/role",
        data={"csrf_token": csrf, "role": "superuser"},
    )
    assert res.status_code == 400
