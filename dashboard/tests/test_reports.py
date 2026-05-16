"""Tests for /api/jobs/{id}/report and the lifecycle transitions it triggers."""

from __future__ import annotations

from app.database import get_db


def test_first_report_promotes_to_suspected(client, test_db_path):
    """One report on an active job should escalate to 'suspected'."""
    r = client.post("/api/jobs/1/report", json={"reason": "not_found"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == 1
    assert body["reported"] is True
    assert body["report_count"] == 1
    assert body["verification_status"] == "suspected"


def test_report_is_idempotent_per_user(client, test_db_path):
    """A user posting the same job twice keeps report_count at 1."""
    r1 = client.post("/api/jobs/1/report", json={"reason": "not_found"})
    r2 = client.post("/api/jobs/1/report", json={"reason": "link_broken"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["report_count"] == 1


def test_distinct_users_increment_count(client, client_b, test_db_path):
    r1 = client.post("/api/jobs/1/report", json={"reason": "not_found"})
    r2 = client_b.post("/api/jobs/1/report", json={"reason": "link_broken"})
    assert r1.json()["report_count"] == 1
    assert r2.json()["report_count"] == 2


def test_withdraw_report(client, test_db_path):
    client.post("/api/jobs/1/report", json={"reason": "not_found"})
    r = client.delete("/api/jobs/1/report")
    assert r.status_code == 200
    assert r.json()["reported"] is False
    assert r.json()["report_count"] == 0


def test_report_invalid_reason(client, test_db_path):
    r = client.post("/api/jobs/1/report", json={"reason": "spam"})
    assert r.status_code == 400


def test_report_missing_job(client, test_db_path):
    r = client.post("/api/jobs/9999/report", json={"reason": "not_found"})
    assert r.status_code == 404


def test_detail_pii_stripped(client, test_db_path):
    r = client.post(
        "/api/jobs/1/report",
        json={"reason": "other", "detail": "Reach me at me@example.com or 555-123-4567"},
    )
    assert r.status_code == 200
    with get_db(test_db_path) as conn:
        row = conn.execute(
            "SELECT detail FROM job_reports WHERE job_id = 1 AND user_id = 1"
        ).fetchone()
    assert row["detail"] is not None
    assert "me@example.com" not in row["detail"]
    assert "555-123-4567" not in row["detail"]
    assert "[email]" in row["detail"] or "[phone]" in row["detail"]


def test_anon_cannot_report(anon_client, test_db_path):
    r = anon_client.post("/api/jobs/1/report", json={"reason": "not_found"})
    assert r.status_code == 401


def test_report_rate_limit_per_day(client, test_db_path, monkeypatch):
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "report_rate_limit_per_day", 2)
    # Reports against different jobs; rate limit is per-user, not per-job
    assert client.post("/api/jobs/1/report", json={"reason": "not_found"}).status_code == 200
    assert client.post("/api/jobs/2/report", json={"reason": "not_found"}).status_code == 200
    r = client.post("/api/jobs/3/report", json={"reason": "not_found"})
    assert r.status_code == 429
