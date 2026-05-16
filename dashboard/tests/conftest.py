from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.database import get_db, init_db, rebuild_fts


# Per-fixture session tokens. The stubbed `verify_request_user` returns the
# payload whose key matches the request's session cookie, so `client` and
# `client_b` can coexist in the same test under one shared resolver.
TOKEN_USER_A = "tok-user-a"
TOKEN_USER_B = "tok-user-b"
TOKEN_USER_C = "tok-user-c"
TOKEN_ADMIN = "tok-admin"

_PAYLOAD_MAP: dict[str, dict | None] = {
    TOKEN_USER_A: {"authenticated": True, "user_id": 1,  "email": "user@test",   "role": "user"},
    TOKEN_USER_B: {"authenticated": True, "user_id": 2,  "email": "user2@test",  "role": "user"},
    TOKEN_USER_C: {"authenticated": True, "user_id": 3,  "email": "user3@test",  "role": "user"},
    TOKEN_ADMIN:  {"authenticated": True, "user_id": 42, "email": "admin@test",  "role": "admin"},
}


@pytest.fixture()
def test_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "applyd-test.db"
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "rolling_window_days", 30)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path)

    # The trailing two columns are verification_status and verification_status_at
    # so seed defaults stay 'active' to avoid drift in pre-existing test counts.
    jobs = [
        (1, "https://jobs.example/1", "Backend Engineer", "Acme", "greenhouse", "US", 1, "FULL_TIME", "Platform", 130000, 160000, "USD", "2026-05-12 10:00:00", "2026-05-12 10:00:00", "active", None),
        (2, "https://jobs.example/2", "Data Engineer", "Beta", "lever", "US", 0, "FULL_TIME", "Data", 90000, 120000, "USD", "2026-05-10 08:00:00", "2026-05-10 08:00:00", "active", None),
        (3, "https://jobs.example/3", "ML Scientist", "Gamma", "workday", "CA", None, "CONTRACT", "Research", None, 210000, "USD", None, "2026-05-09 07:00:00", "active", None),
        (4, "https://jobs.example/4", "Frontend Engineer", "Acme", "greenhouse", "US", 1, "FULL_TIME", "Product", 70000, 95000, "USD", "2026-05-01 09:00:00", "2026-05-01 09:00:00", "active", None),
        (5, "https://jobs.example/5", "Future Role", "FutureCorp", "workday", "US", 1, "FULL_TIME", "Ops", 100000, 140000, "USD", "2030-01-01 00:00:00", "2026-05-12 09:00:00", "active", None),
        (6, "https://jobs.example/6", "Staff Engineer", "Delta", "icims", "GB", 0, "PART_TIME", "Infra", 200000, 260000, "USD", "2026-04-20 12:00:00", "2026-04-20 12:00:00", "active", None),
    ]

    with get_db(db_path) as conn:
        conn.executemany(
            "INSERT INTO users (id, name, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            [
                (1,  "User One",  "user@test",   "x", "user"),
                (2,  "User Two",  "user2@test",  "x", "user"),
                (3,  "User Three","user3@test",  "x", "user"),
                (42, "Admin",     "admin@test",  "x", "admin"),
            ],
        )

        for row in jobs:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, url, title, company, ats_type, country, is_remote, employment_type,
                    department, salary_min_usd_annual, salary_max_usd_annual, salary_currency,
                    posted_at, first_seen_at, location, description, apply_url, salary_summary,
                    verification_status, verification_status_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                    row[8], row[9], row[10], row[11], row[12], row[13], "Remote", "role details", row[1] + "/apply", "$100k+",
                    row[14], row[15],
                ),
            )

        conn.executemany(
            "INSERT INTO saved_jobs (user_id, job_id, notes, status, saved_at) VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, "priority", "queued",  "2026-05-12 12:00:00"),
                (1, 2, "done",     "applied", "2026-05-11 12:00:00"),
                (1, 3, "skip",     "skipped", "2026-05-10 12:00:00"),
            ],
        )

        conn.executemany(
            """
            INSERT INTO manifest_log (
                fetched_at, manifest_updated_at, total_jobs_upstream, ats_count,
                rows_ingested, rows_pruned, status, error, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("2026-05-12 11:00:00", "2026-05-12 10:59:00", 6, 4, 6, 0, "success", None, 1.2),
                ("2026-05-11 11:00:00", "2026-05-11 10:59:00", 5, 4, 0, 1, "failed", "network", 2.0),
            ],
        )
        rebuild_fts(conn)

    return db_path


def _install_identity_stub_by_token(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, dict | None]) -> None:
    """Resolve identity from the request's session cookie. Enables multi-user tests."""
    from app import main

    def resolver(request):
        token = request.cookies.get(settings.session_cookie_name)
        return mapping.get(token)
    monkeypatch.setattr(main, "verify_request_user", resolver)


@pytest.fixture()
def _stubbed_app(test_db_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app import main
    monkeypatch.setattr(main, "start_scheduler", lambda: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)
    _install_identity_stub_by_token(monkeypatch, _PAYLOAD_MAP)
    return main


@pytest.fixture()
def client(_stubbed_app) -> TestClient:
    with TestClient(_stubbed_app.app, raise_server_exceptions=False) as test_client:
        test_client.cookies.set(settings.session_cookie_name, TOKEN_USER_A)
        yield test_client


@pytest.fixture()
def client_b(_stubbed_app) -> TestClient:
    with TestClient(_stubbed_app.app, raise_server_exceptions=False) as test_client:
        test_client.cookies.set(settings.session_cookie_name, TOKEN_USER_B)
        yield test_client


@pytest.fixture()
def client_c(_stubbed_app) -> TestClient:
    """Third user — exercises the two-distinct-reporters rule."""
    with TestClient(_stubbed_app.app, raise_server_exceptions=False) as test_client:
        test_client.cookies.set(settings.session_cookie_name, TOKEN_USER_C)
        yield test_client


@pytest.fixture()
def admin_client(_stubbed_app) -> TestClient:
    """Same shape as `client` but the identity stub returns role=admin."""
    with TestClient(_stubbed_app.app, raise_server_exceptions=False) as test_client:
        test_client.cookies.set(settings.session_cookie_name, TOKEN_ADMIN)
        test_client.cookies.set("applyd_csrf", "test-csrf")
        yield test_client


@pytest.fixture()
def anon_client(_stubbed_app) -> TestClient:
    """No session cookie — exercises the 401/redirect paths."""
    with TestClient(_stubbed_app.app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def lifecycle_seed(test_db_path):
    """Opt-in: seeds one 'suspected' and one 'expired' job (ids 7, 8).

    Lives behind a fixture so the default seed keeps stable row counts for
    pre-existing tests. Tests for the availability filter / admin
    moderation explicitly request this fixture.
    """
    with get_db(test_db_path) as conn:
        conn.execute(
            "INSERT INTO jobs (id, url, title, company, ats_type, country, "
            "is_remote, employment_type, department, salary_min_usd_annual, "
            "salary_max_usd_annual, salary_currency, posted_at, first_seen_at, "
            "location, description, apply_url, salary_summary, "
            "verification_status, verification_status_at) "
            "VALUES (7, 'https://jobs.example/7', 'Suspected Role', 'Echo', "
            "'ashby', 'US', 1, 'FULL_TIME', 'Platform', 110000, 140000, 'USD', "
            "'2026-05-11 10:00:00', '2026-05-11 10:00:00', 'Remote', 'd', "
            "'https://jobs.example/7/apply', '$100k+', 'suspected', "
            "'2026-05-14 09:00:00')"
        )
        conn.execute(
            "INSERT INTO jobs (id, url, title, company, ats_type, country, "
            "is_remote, employment_type, department, salary_min_usd_annual, "
            "salary_max_usd_annual, salary_currency, posted_at, first_seen_at, "
            "location, description, apply_url, salary_summary, "
            "verification_status, verification_status_at) "
            "VALUES (8, 'https://jobs.example/8', 'Expired Role', 'Foxtrot', "
            "'lever', 'US', 0, 'FULL_TIME', 'Sales', 80000, 100000, 'USD', "
            "'2026-05-08 10:00:00', '2026-05-08 10:00:00', 'Remote', 'd', "
            "'https://jobs.example/8/apply', '$80k+', 'expired', "
            "'2026-05-13 09:00:00')"
        )
    return test_db_path
