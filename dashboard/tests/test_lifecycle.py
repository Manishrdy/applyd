"""Direct tests of the job_lifecycle state machine.

Sit below the HTTP layer — feed signals into job_lifecycle.* and assert
the resulting jobs.verification_status + job_verification_log rows.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.database import get_db
from app.services import job_lifecycle


@pytest.fixture(autouse=True)
def _enable_auto_marking(monkeypatch):
    """Lifecycle tests assert end-state promotion; flip the kill switch."""
    monkeypatch.setattr(settings, "verifier_auto_marking_enabled", True)


def _status(conn, job_id):
    return str(conn.execute(
        "SELECT verification_status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["verification_status"])


def test_user_report_active_to_suspected(test_db_path):
    with get_db(test_db_path) as conn:
        conn.execute(
            "INSERT INTO job_reports (user_id, job_id, reason) VALUES (1, 1, 'not_found')"
        )
        conn.execute("UPDATE jobs SET report_count = 1 WHERE id = 1")
        job_lifecycle.on_user_report(conn, 1)
        assert _status(conn, 1) == "suspected"


def test_user_report_two_distinct_alone_stays_suspected(test_db_path):
    """Two user reports without manifest-drop corroboration should NOT promote."""
    with get_db(test_db_path) as conn:
        for uid in (1, 2):
            conn.execute(
                "INSERT INTO job_reports (user_id, job_id, reason) VALUES (?, 1, 'not_found')",
                (uid,),
            )
        conn.execute("UPDATE jobs SET report_count = 2 WHERE id = 1")
        job_lifecycle.on_user_report(conn, 1)  # first promotion
        job_lifecycle.on_user_report(conn, 1)  # would-promote
        assert _status(conn, 1) == "suspected"


def test_user_reports_plus_manifest_drop_promotes(test_db_path):
    with get_db(test_db_path) as conn:
        conn.execute("UPDATE jobs SET missed_ingest_cycles = 2 WHERE id = 1")
        for uid in (1, 2):
            conn.execute(
                "INSERT INTO job_reports (user_id, job_id, reason) VALUES (?, 1, 'not_found')",
                (uid,),
            )
        conn.execute("UPDATE jobs SET report_count = 2 WHERE id = 1")
        # First report bumps to suspected; second corroborated by drops promotes.
        job_lifecycle.on_user_report(conn, 1)
        job_lifecycle.on_user_report(conn, 1)
        assert _status(conn, 1) == "expired"


def test_manifest_drop_alone_only_suspects(test_db_path):
    with get_db(test_db_path) as conn:
        job_lifecycle.on_manifest_drop(conn, 1, missed=3)
        assert _status(conn, 1) == "suspected"


def test_http_404_jumps_straight_to_expired(test_db_path):
    with get_db(test_db_path) as conn:
        job_lifecycle.on_http_check(
            conn, 1, result="expired", http_status=404,
            detector="match_greenhouse", detail="HTTP 404",
        )
        assert _status(conn, 1) == "expired"


def test_http_active_downgrades_suspected(lifecycle_seed):
    """A live HTTP response on a suspected job should reactivate it."""
    with get_db(lifecycle_seed) as conn:
        job_lifecycle.on_http_check(
            conn, 7, result="active", http_status=200,
            detector="match_ashby", detail="matcher: active",
        )
        assert _status(conn, 7) == "active"


def test_kill_switch_blocks_all_transitions(test_db_path, monkeypatch):
    monkeypatch.setattr(settings, "expired_detection_enabled", False)
    with get_db(test_db_path) as conn:
        prior = _status(conn, 1)
        result = job_lifecycle.on_http_check(
            conn, 1, result="expired", http_status=404,
            detector="match_greenhouse", detail="HTTP 404",
        )
        assert result is None
        assert _status(conn, 1) == prior


def test_auto_marking_disabled_blocks_expire_only(test_db_path, monkeypatch):
    """With auto-marking off, HTTP 'expired' is logged but no status flips."""
    monkeypatch.setattr(settings, "verifier_auto_marking_enabled", False)
    with get_db(test_db_path) as conn:
        prior = _status(conn, 1)
        job_lifecycle.on_http_check(
            conn, 1, result="expired", http_status=404,
            detector="match_greenhouse", detail="HTTP 404",
        )
        assert _status(conn, 1) == prior
        log_row = conn.execute(
            "SELECT result FROM job_verification_log WHERE job_id = 1 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert log_row["result"] == "expired"
