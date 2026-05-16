"""Admin moderation API and observability summary."""

from __future__ import annotations

from app.database import get_db


def _seed_report(test_db_path, user_id=1, job_id=1, reason="not_found"):
    with get_db(test_db_path) as conn:
        conn.execute(
            "INSERT INTO job_reports (user_id, job_id, reason) VALUES (?, ?, ?)",
            (user_id, job_id, reason),
        )
        conn.execute("UPDATE jobs SET report_count = report_count + 1 WHERE id = ?", (job_id,))


def test_admin_list_requires_admin(client, test_db_path):
    r = client.get("/api/admin/job-reports")
    assert r.status_code == 403


def test_admin_list_returns_reported_jobs(admin_client, test_db_path):
    _seed_report(test_db_path, user_id=1, job_id=1)
    r = admin_client.get("/api/admin/job-reports?min_reports=1")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [row["id"] for row in body["reports"]]
    assert 1 in ids


def test_admin_filter_status_expired(admin_client, lifecycle_seed):
    r = admin_client.get("/api/admin/job-reports?status=expired&min_reports=0")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["reports"]]
    assert 8 in ids  # seeded expired
    assert 1 not in ids


def test_admin_reactivate_flips_and_audits(admin_client, lifecycle_seed):
    r = admin_client.post("/api/admin/jobs/8/reactivate", data={"csrf_token": "test-csrf"})
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "active"
    with get_db(lifecycle_seed) as conn:
        row = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = 8"
        ).fetchone()
        assert row["verification_status"] == "active"
        audit = conn.execute(
            "SELECT action, target FROM admin_audit "
            "WHERE action = 'reactivate_job' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert audit is not None
        assert audit["target"] == "8"


def test_admin_force_expire(admin_client, test_db_path):
    r = admin_client.post("/api/admin/jobs/1/expire", data={"csrf_token": "test-csrf"})
    assert r.status_code == 200
    with get_db(test_db_path) as conn:
        status = conn.execute(
            "SELECT verification_status FROM jobs WHERE id = 1"
        ).fetchone()["verification_status"]
        assert status == "expired"


def test_admin_detail_endpoint(admin_client, test_db_path):
    _seed_report(test_db_path, user_id=1, job_id=1)
    r = admin_client.get("/api/admin/job-reports/1")
    assert r.status_code == 200
    body = r.json()
    assert body["job"]["id"] == 1
    assert len(body["reports"]) >= 1


def test_admin_anomalous_reporters(admin_client, test_db_path):
    for jid in (1, 2, 3, 4):
        _seed_report(test_db_path, user_id=1, job_id=jid)
    r = admin_client.get("/api/admin/job-reports-reporters?min_reports=2")
    assert r.status_code == 200
    users = [row["user_id"] for row in r.json()["reporters"]]
    assert 1 in users


def test_expirations_summary(admin_client, lifecycle_seed):
    r = admin_client.get("/api/admin/expirations/summary")
    assert r.status_code == 200
    body = r.json()
    counts = body["counts"]
    assert counts.get("active", 0) >= 1
    assert counts.get("suspected", 0) >= 1
    assert counts.get("expired", 0) >= 1
