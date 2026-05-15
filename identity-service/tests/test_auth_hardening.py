from __future__ import annotations


def _csrf_from_client(client) -> str:
    res = client.get("/signin")
    assert res.status_code == 200
    token = client.cookies.get("applyd_csrf")
    assert token
    return token


def test_signin_page_sets_csrf_cookie(client):
    res = client.get("/signin")
    assert res.status_code == 200
    assert client.cookies.get("applyd_csrf")


def test_signup_page_renders_csrf_hidden_input(client):
    res = client.get("/signup")
    assert res.status_code == 200
    csrf = client.cookies.get("applyd_csrf")
    assert csrf
    assert f'name="csrf_token" value="{csrf}"' in res.text


def test_signup_requires_csrf(client):
    res = client.post(
        "/signup",
        data={"name": "Test User", "email": "test@example.com", "password": "Goodpass123!"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/signup?error=invalid_request"


def test_signup_success_sets_session_cookie(client):
    csrf = _csrf_from_client(client)
    res = client.post(
        "/signup",
        data={
            "name": "Test User",
            "email": "test@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/dashboard"
    set_cookie = res.headers.get("set-cookie", "")
    assert "applyd_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


def test_signin_blocks_open_redirect_to_external_host(client):
    csrf = _csrf_from_client(client)
    client.post(
        "/signup",
        data={
            "name": "Test User",
            "email": "test@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    csrf = _csrf_from_client(client)
    res = client.post(
        "/signin",
        data={
            "email": "test@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "https://evil.example/phish",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "http://testserver:8000/dashboard"


def test_signin_allows_safe_relative_redirect(client):
    csrf = _csrf_from_client(client)
    client.post(
        "/signup",
        data={
            "name": "Test User",
            "email": "test@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    csrf = _csrf_from_client(client)
    res = client.post(
        "/signin",
        data={
            "email": "test@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/saved",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/saved"


def test_signin_rate_limit_locks_after_failed_attempts(client):
    csrf = _csrf_from_client(client)
    for _ in range(5):
        res = client.post(
            "/signin",
            data={
                "email": "nobody@example.com",
                "password": "wrong-password",
                "csrf_token": csrf,
                "next": "/dashboard",
            },
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert res.headers["location"] == "/signin?error=invalid_credentials"

    res = client.post(
        "/signin",
        data={
            "email": "nobody@example.com",
            "password": "wrong-password",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/signin?error=rate_limited"


def test_cookie_policy_uses_secure_when_enabled(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "session_cookie_secure", True)
    monkeypatch.setattr(main.settings, "session_cookie_samesite", "strict")

    csrf = _csrf_from_client(client)
    res = client.post(
        "/signup",
        data={
            "name": "Secure User",
            "email": "secure@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    set_cookie = res.headers.get("set-cookie", "")
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie


def test_signup_rejects_weak_password(client):
    csrf = _csrf_from_client(client)
    res = client.post(
        "/signup",
        data={
            "name": "Weak User",
            "email": "weak@example.com",
            "password": "weakpass",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/signup?error=weak_password"


def test_logout_requires_csrf(client):
    csrf = _csrf_from_client(client)
    client.post(
        "/signup",
        data={
            "name": "Test User",
            "email": "logout@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    res = client.post("/logout", data={}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/signin?error=invalid_request"


def test_auth_sessions_list_uses_session_id_not_raw_token(client):
    csrf = _csrf_from_client(client)
    client.post(
        "/signup",
        data={
            "name": "Session User",
            "email": "sessionid@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    sessions_res = client.get("/api/auth/sessions")
    assert sessions_res.status_code == 200
    sessions = sessions_res.json()["sessions"]
    assert len(sessions) >= 1
    row = sessions[0]
    assert "session_id" in row and row["session_id"]
    assert "token" not in row


def test_signin_rate_limit_uses_x_forwarded_for_when_proxy_trusted(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "trusted_proxy_hops", 1)
    csrf = _csrf_from_client(client)
    for _ in range(5):
        client.post(
            "/signin",
            data={
                "email": "xff@example.com",
                "password": "wrong-password",
                "csrf_token": csrf,
                "next": "/dashboard",
            },
            headers={"X-Forwarded-For": "203.0.113.50"},
            follow_redirects=False,
        )
    res = client.post(
        "/signin",
        data={
            "email": "xff@example.com",
            "password": "wrong-password",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        headers={"X-Forwarded-For": "203.0.113.50"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/signin?error=rate_limited"

    csrf2 = _csrf_from_client(client)
    res_other = client.post(
        "/signin",
        data={
            "email": "xff@example.com",
            "password": "wrong-password",
            "csrf_token": csrf2,
            "next": "/dashboard",
        },
        headers={"X-Forwarded-For": "203.0.113.99"},
        follow_redirects=False,
    )
    assert res_other.headers["location"] == "/signin?error=invalid_credentials"


def test_auth_sessions_and_revoke_all(client):
    csrf = _csrf_from_client(client)
    client.post(
        "/signup",
        data={
            "name": "Session User",
            "email": "session@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    sessions_res = client.get("/api/auth/sessions")
    assert sessions_res.status_code == 200
    sessions = sessions_res.json()["sessions"]
    assert len(sessions) >= 1

    # Revoke-all now requires re-entering the password, so a missing /
    # wrong password must be rejected even when CSRF is valid.
    no_password = client.post(
        "/api/auth/sessions/revoke-all",
        data={"csrf_token": csrf},
    )
    assert no_password.status_code == 403

    wrong_password = client.post(
        "/api/auth/sessions/revoke-all",
        data={"csrf_token": csrf, "password": "Wrongpass123!"},
    )
    assert wrong_password.status_code == 403

    revoke_res = client.post(
        "/api/auth/sessions/revoke-all",
        data={"csrf_token": csrf, "password": "Goodpass123!"},
    )
    assert revoke_res.status_code == 200
    assert revoke_res.json()["revoked"] >= 0


def test_security_headers_present(client):
    res = client.get("/signin")
    assert res.status_code == 200
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert res.headers.get("Referrer-Policy")
    csp = res.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "form-action 'self'" in csp


def test_csrf_cookie_not_rotated_across_requests(client):
    res1 = client.get("/signin")
    assert res1.status_code == 200
    token1 = client.cookies.get("applyd_csrf")
    assert token1
    res2 = client.get("/signin")
    assert res2.status_code == 200
    token2 = client.cookies.get("applyd_csrf")
    assert token2 == token1, "CSRF cookie must persist across tabs / GETs"


def test_session_token_stored_hashed_at_rest(client, tmp_path):
    csrf = _csrf_from_client(client)
    res = client.post(
        "/signup",
        data={
            "name": "Hash User",
            "email": "hash@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    # Extract the raw token from the Set-Cookie header.
    set_cookie = res.headers.get("set-cookie", "")
    raw_token = ""
    for chunk in set_cookie.split(";"):
        chunk = chunk.strip()
        if chunk.startswith("applyd_session="):
            raw_token = chunk.split("=", 1)[1]
            break
    assert raw_token

    import sqlite3
    from app import main
    conn = sqlite3.connect(str(main.settings.db_path))
    rows = [r[0] for r in conn.execute("SELECT token FROM auth_sessions").fetchall()]
    conn.close()
    assert rows
    # No raw token should appear in the DB column.
    assert raw_token not in rows
    # Every stored value should look like a sha256 hex digest.
    for stored in rows:
        assert len(stored) == 64
        int(stored, 16)  # raises ValueError if not hex


def test_signup_ip_rate_limit(client, monkeypatch):
    from app import main
    monkeypatch.setattr(main.settings, "auth_signup_ip_max_attempts", 3)
    csrf = _csrf_from_client(client)
    # First 3 weak-password attempts are allowed (each counted).
    for i in range(3):
        client.post(
            "/signup",
            data={
                "name": f"User {i}",
                "email": f"user{i}@example.com",
                "password": "weakpass",
                "csrf_token": csrf,
                "next": "/dashboard",
            },
            follow_redirects=False,
        )
    # 4th is locked out at the IP level — regardless of password strength.
    res = client.post(
        "/signup",
        data={
            "name": "Late User",
            "email": "late@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/signup?error=signup_rate_limited"


def test_signup_assigns_default_user_role(client):
    csrf = _csrf_from_client(client)
    res = client.post(
        "/signup",
        data={
            "name": "Role User",
            "email": "role-default@example.com",
            "password": "Goodpass123!",
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303

    import sqlite3
    from app import main
    conn = sqlite3.connect(str(main.settings.db_path))
    row = conn.execute(
        "SELECT role FROM users WHERE email = ?",
        ("role-default@example.com",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "user"


def test_create_user_persists_admin_role(client):
    from app.auth import create_user, get_user_role

    user_id = create_user(
        name="Admin User",
        email="admin@example.com",
        password="Adminpass123!",
        role="admin",
    )
    assert get_user_role(user_id) == "admin"


def test_create_user_rejects_unknown_role(client):
    import pytest as _pytest
    from app.auth import create_user

    with _pytest.raises(ValueError):
        create_user(
            name="Bad Role",
            email="badrole@example.com",
            password="Goodpass123!",
            role="superadmin",
        )


def test_require_admin_blocks_non_admin(client):
    from fastapi import HTTPException
    import pytest as _pytest
    from app.auth import create_user, require_admin

    regular_id = create_user(
        name="Reg User",
        email="reg@example.com",
        password="Goodpass123!",
    )
    with _pytest.raises(HTTPException) as exc:
        require_admin(regular_id)
    assert exc.value.status_code == 403

    admin_id = create_user(
        name="Admin User",
        email="admin2@example.com",
        password="Goodpass123!",
        role="admin",
    )
    require_admin(admin_id)  # must not raise


def test_legacy_pbkdf2_hash_is_upgraded_on_signin(client):
    """A user created on the old PBKDF2 scheme must still be able to sign in,
    and the hash must transparently upgrade to Argon2id afterwards."""
    import hashlib, os, sqlite3
    from app import main

    # Hand-craft a legacy PBKDF2 row that pre-dates the Argon2 migration.
    plaintext = "Legacypass123!"
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, 120_000).hex()
    legacy_hash = f"{salt.hex()}:{digest}"
    conn = sqlite3.connect(str(main.settings.db_path))
    conn.execute(
        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
        ("Legacy User", "legacy@example.com", legacy_hash),
    )
    conn.commit()
    conn.close()

    csrf = _csrf_from_client(client)
    res = client.post(
        "/signin",
        data={
            "email": "legacy@example.com",
            "password": plaintext,
            "csrf_token": csrf,
            "next": "/dashboard",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/dashboard"

    # Hash should now be Argon2id, not the legacy "<salt>:<digest>" format.
    conn = sqlite3.connect(str(main.settings.db_path))
    row = conn.execute(
        "SELECT password_hash FROM users WHERE email = ?",
        ("legacy@example.com",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0].startswith("$argon2id$")
