"""Tests for admin_clear_failed_logins: rate limits + auth_events."""

from __future__ import annotations

from pathlib import Path

from app.database import get_db
from app.identity import auth


def _seed_auth_events(conn) -> None:
    conn.executemany(
        "INSERT INTO auth_events (event_type, email, user_id, ip_address, user_agent, success, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("signin", "aaa@exam ple.com", None, "203.0.113.1", None, 0, "bad"),
            ("signin", "bbb@test", None, "203.0.113.2", None, 0, "bad2"),
            ("signin", "aaa@exam ple.com", 1, "203.0.113.1", None, 1, "ok"),
            ("signin", None, None, "203.0.113.9", None, 0, "no email"),
        ],
    )


def test_broad_clear_deletes_only_failed_events(test_db_path: Path):
    with get_db(test_db_path) as conn:
        _seed_auth_events(conn)

    out = auth.admin_clear_failed_logins()
    assert out["events_deleted"] == 3
    assert out["cleared"] == 0

    with get_db(test_db_path) as conn:
        n_fail = conn.execute("SELECT COUNT(*) FROM auth_events WHERE success = 0").fetchone()[0]
        n_ok = conn.execute("SELECT COUNT(*) FROM auth_events WHERE success = 1").fetchone()[0]
    assert n_fail == 0
    assert n_ok == 1


def test_scoped_clear_email(test_db_path: Path):
    with get_db(test_db_path) as conn:
        _seed_auth_events(conn)

    out = auth.admin_clear_failed_logins(email="aaa@exam ple.com")
    assert out["events_deleted"] == 1

    with get_db(test_db_path) as conn:
        emails = [r[0] for r in conn.execute("SELECT email FROM auth_events ORDER BY id").fetchall()]
        failed_emails = [
            r[0]
            for r in conn.execute("SELECT email FROM auth_events WHERE success = 0").fetchall()
        ]

    assert "aaa@exam ple.com" in emails  # success=1 row kept
    assert failed_emails == ["bbb@test", None]


def test_scoped_clear_ip(test_db_path: Path):
    with get_db(test_db_path) as conn:
        _seed_auth_events(conn)

    out = auth.admin_clear_failed_logins(ip_address="203.0.113.9")
    assert out["events_deleted"] == 1

    with get_db(test_db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM auth_events WHERE success = 0 AND ip_address = '203.0.113.9'"
        ).fetchone()[0]
    assert n == 0


def test_scoped_clear_email_or_ip(test_db_path: Path):
    with get_db(test_db_path) as conn:
        _seed_auth_events(conn)

    out = auth.admin_clear_failed_logins(email="bbb@test", ip_address="203.0.113.9")
    assert out["events_deleted"] == 2

    with get_db(test_db_path) as conn:
        n_fail = conn.execute("SELECT COUNT(*) FROM auth_events WHERE success = 0").fetchone()[0]
    assert n_fail == 1  # only aaa failed left
