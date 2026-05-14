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

    jobs = [
        (1, "https://jobs.example/1", "Backend Engineer", "Acme", "greenhouse", "US", 1, "FULL_TIME", "Platform", 130000, 160000, "USD", "2026-05-12 10:00:00", "2026-05-12 10:00:00"),
        (2, "https://jobs.example/2", "Data Engineer", "Beta", "lever", "US", 0, "FULL_TIME", "Data", 90000, 120000, "USD", "2026-05-10 08:00:00", "2026-05-10 08:00:00"),
        (3, "https://jobs.example/3", "ML Scientist", "Gamma", "workday", "CA", None, "CONTRACT", "Research", None, 210000, "USD", None, "2026-05-09 07:00:00"),
        (4, "https://jobs.example/4", "Frontend Engineer", "Acme", "greenhouse", "US", 1, "FULL_TIME", "Product", 70000, 95000, "USD", "2026-05-01 09:00:00", "2026-05-01 09:00:00"),
        (5, "https://jobs.example/5", "Future Role", "FutureCorp", "workday", "US", 1, "FULL_TIME", "Ops", 100000, 140000, "USD", "2030-01-01 00:00:00", "2026-05-12 09:00:00"),
        (6, "https://jobs.example/6", "Staff Engineer", "Delta", "icims", "GB", 0, "PART_TIME", "Infra", 200000, 260000, "USD", "2026-04-20 12:00:00", "2026-04-20 12:00:00"),
    ]

    with get_db(db_path) as conn:
        for row in jobs:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, url, title, company, ats_type, country, is_remote, employment_type,
                    department, salary_min_usd_annual, salary_max_usd_annual, salary_currency,
                    posted_at, first_seen_at, location, description, apply_url, salary_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                    row[8], row[9], row[10], row[11], row[12], row[13], "Remote", "role details", row[1] + "/apply", "$100k+",
                ),
            )

        conn.executemany(
            "INSERT INTO saved_jobs (job_id, notes, status, saved_at) VALUES (?, ?, ?, ?)",
            [
                (1, "priority", "queued", "2026-05-12 12:00:00"),
                (2, "done", "applied", "2026-05-11 12:00:00"),
                (3, "skip", "skipped", "2026-05-10 12:00:00"),
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


@pytest.fixture()
def client(test_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app import main

    monkeypatch.setattr(main, "start_scheduler", lambda: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)

    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client
