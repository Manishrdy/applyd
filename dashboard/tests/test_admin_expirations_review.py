"""Tests for the expired-job review + bulk-cleanup admin surface."""

from __future__ import annotations

import json

from app.database import get_db


# ─── Seed helpers ──────────────────────────────────────────────────────────


def _seed_expired_jobs(test_db_path) -> list[int]:
    """Seed an additional set of expired jobs across two ATSes + countries
    so the filter & group-stats tests have data to slice on. Returns the
    ids that were inserted."""
    seeds = [
        # (id, url, title, ats, country, company, status_at)
        (901, "https://jobs.example/901", "Closed GH 1", "greenhouse", "US", "Acme",  "2026-05-10 09:00:00"),
        (902, "https://jobs.example/902", "Closed GH 2", "greenhouse", "US", "Acme",  "2026-05-11 09:00:00"),
        (903, "https://jobs.example/903", "Closed GH 3", "greenhouse", "GB", "Acme",  "2026-05-12 09:00:00"),
        (904, "https://jobs.example/904", "Closed LV 1", "lever",      "US", "Beta",  "2026-05-13 09:00:00"),
        (905, "https://jobs.example/905", "Closed LV 2", "lever",      "GB", "Beta",  "2026-05-14 09:00:00"),
    ]
    with get_db(test_db_path) as conn:
        for (jid, url, title, ats, country, company, status_at) in seeds:
            conn.execute(
                "INSERT INTO jobs (id, url, title, company, ats_type, country, "
                "is_remote, employment_type, posted_at, first_seen_at, "
                "verification_status, verification_status_at, location, description) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 'FULL_TIME', '2026-05-08 09:00:00', "
                "'2026-05-08 09:00:00', 'expired', ?, 'Remote', 'd')",
                (jid, url, title, company, ats, country, status_at),
            )
        # Plant a verification_log row tying each expired row to a trigger +
        # detector so the reason/detector filters exercise their joins.
        conn.execute(
            "INSERT INTO job_verification_log (job_id, trigger, result, detector, detail) "
            "VALUES "
            "(901, 'http_check', 'expired', 'match_greenhouse', 'HTTP 404'),"
            "(902, 'http_check', 'expired', 'match_greenhouse', 'HTTP 404'),"
            "(903, 'user_report', 'expired', NULL,              'promoted on 2 reports + drop'),"
            "(904, 'http_check', 'expired', 'match_lever',      'HTTP 410'),"
            "(905, 'manifest_drop','expired', NULL,             'corroborated drop')"
        )
    return [s[0] for s in seeds]


# ─── /api/admin/expirations/summary ────────────────────────────────────────


def test_summary_returns_complete_payload(admin_client, lifecycle_seed):
    r = admin_client.get("/api/admin/expirations/summary")
    assert r.status_code == 200
    body = r.json()
    for key in ("counts", "today", "last_hour", "last_24h",
                "per_ats", "per_detector", "schedule", "breakers",
                "recent", "last_tick"):
        assert key in body, f"missing key: {key}"
    assert body["counts"].get("expired", 0) >= 1


def test_summary_schedule_exposes_kill_switches(admin_client, test_db_path):
    r = admin_client.get("/api/admin/expirations/summary")
    sched = r.json()["schedule"]
    assert "expired_detection_enabled" in sched
    assert "verifier_auto_marking_enabled" in sched
    assert "sweep_all_active" in sched
    assert sched["sweep_days"] >= 1


# ─── /api/admin/expirations/review ─────────────────────────────────────────


def test_review_filters_by_ats(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.get("/api/admin/expirations/review?ats=greenhouse")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert all(j["ats_type"] == "greenhouse" for j in body["jobs"])


def test_review_filters_by_country(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.get("/api/admin/expirations/review?country=GB")
    assert r.status_code == 200
    ids = sorted(j["id"] for j in r.json()["jobs"])
    assert ids == [903, 905]


def test_review_filters_by_reason(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.get("/api/admin/expirations/review?reason=manifest_drop")
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()["jobs"]]
    assert ids == [905]


def test_review_filters_by_detector(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.get("/api/admin/expirations/review?detector=match_lever")
    assert r.status_code == 200
    ids = [j["id"] for j in r.json()["jobs"]]
    assert ids == [904]


def test_review_group_stats(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.get("/api/admin/expirations/review")
    body = r.json()
    by_ats = {row["ats_type"]: row["n"] for row in body["group_stats"]["by_ats"]}
    by_country = {row["country"]: row["n"] for row in body["group_stats"]["by_country"]}
    assert by_ats["greenhouse"] == 3
    assert by_ats["lever"] == 2
    assert by_country["US"] == 3
    assert by_country["GB"] == 2


def test_review_pagination(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    p1 = admin_client.get("/api/admin/expirations/review?limit=2&offset=0&sort=oldest").json()
    p2 = admin_client.get("/api/admin/expirations/review?limit=2&offset=2&sort=oldest").json()
    assert p1["jobs"] != p2["jobs"]
    assert p1["total"] == p2["total"]


def test_review_requires_admin(client, test_db_path):
    r = client.get("/api/admin/expirations/review")
    assert r.status_code == 403


# ─── /api/admin/expirations/bulk-delete ────────────────────────────────────


def test_bulk_delete_requires_count_match(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    # Wrong count → refused.
    r = admin_client.post(
        "/api/admin/expirations/bulk-delete",
        data={
            "filters_json": json.dumps({"ats": ["greenhouse"]}),
            "confirm_count": 99,
            "csrf_token": "test-csrf",
        },
    )
    assert r.status_code == 400
    assert "confirm_count mismatch" in r.json()["detail"]
    with get_db(test_db_path) as conn:
        assert int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id IN (901, 902, 903)"
        ).fetchone()[0]) == 3


def test_bulk_delete_succeeds_with_correct_count(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.post(
        "/api/admin/expirations/bulk-delete",
        data={
            "filters_json": json.dumps({"ats": ["greenhouse"]}),
            "confirm_count": 3,
            "csrf_token": "test-csrf",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 3
    with get_db(test_db_path) as conn:
        remaining = int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id IN (901, 902, 903)"
        ).fetchone()[0])
        assert remaining == 0
        # lever rows untouched.
        assert int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id IN (904, 905)"
        ).fetchone()[0]) == 2


def test_bulk_delete_cascades_saved_jobs(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    with get_db(test_db_path) as conn:
        conn.execute(
            "INSERT INTO saved_jobs (user_id, job_id, status) VALUES (1, 901, 'queued')"
        )
    admin_client.post(
        "/api/admin/expirations/bulk-delete",
        data={
            "filters_json": json.dumps({"ats": ["greenhouse"]}),
            "confirm_count": 3,
            "csrf_token": "test-csrf",
        },
    )
    with get_db(test_db_path) as conn:
        saved = int(conn.execute(
            "SELECT COUNT(*) FROM saved_jobs WHERE job_id = 901"
        ).fetchone()[0])
        assert saved == 0


def test_bulk_delete_preserves_job_reports(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    with get_db(test_db_path) as conn:
        conn.execute(
            "INSERT INTO job_reports (user_id, job_id, reason) "
            "VALUES (1, 904, 'not_found')"
        )
    admin_client.post(
        "/api/admin/expirations/bulk-delete",
        data={
            "filters_json": json.dumps({"ats": ["lever"]}),
            "confirm_count": 2,
            "csrf_token": "test-csrf",
        },
    )
    with get_db(test_db_path) as conn:
        row = conn.execute(
            "SELECT job_id FROM job_reports WHERE user_id = 1 AND reason = 'not_found'"
        ).fetchone()
        assert row is not None
        # job_id SET NULL, not deleted.
        assert row["job_id"] is None


def test_bulk_delete_writes_admin_audit(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    admin_client.post(
        "/api/admin/expirations/bulk-delete",
        data={
            "filters_json": json.dumps({"ats": ["lever"]}),
            "confirm_count": 2,
            "csrf_token": "test-csrf",
        },
    )
    with get_db(test_db_path) as conn:
        row = conn.execute(
            "SELECT action, target, detail FROM admin_audit "
            "WHERE action = 'bulk_delete_expired' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert "2 jobs" in str(row["target"])
        assert "lever" in str(row["detail"])


def test_bulk_delete_requires_admin(client, test_db_path):
    r = client.post(
        "/api/admin/expirations/bulk-delete",
        data={"filters_json": "{}", "confirm_count": 0, "csrf_token": "x"},
    )
    assert r.status_code == 403


# ─── /api/admin/expirations/bulk-reactivate ────────────────────────────────


def test_bulk_reactivate_flips_status(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.post(
        "/api/admin/expirations/bulk-reactivate",
        data={
            "filters_json": json.dumps({"ats": ["lever"]}),
            "confirm_count": 2,
            "csrf_token": "test-csrf",
        },
    )
    assert r.status_code == 200
    assert r.json()["reactivated"] == 2
    with get_db(test_db_path) as conn:
        statuses = [
            r["verification_status"]
            for r in conn.execute(
                "SELECT verification_status FROM jobs WHERE id IN (904, 905)"
            ).fetchall()
        ]
        assert statuses == ["active", "active"]


def test_bulk_reactivate_writes_admin_audit(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    admin_client.post(
        "/api/admin/expirations/bulk-reactivate",
        data={
            "filters_json": json.dumps({"ats": ["greenhouse"]}),
            "confirm_count": 3,
            "csrf_token": "test-csrf",
        },
    )
    with get_db(test_db_path) as conn:
        row = conn.execute(
            "SELECT action, target FROM admin_audit "
            "WHERE action = 'bulk_reactivate_expired' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert "3 jobs" in str(row["target"])


def test_bulk_reactivate_count_mismatch_refused(admin_client, test_db_path):
    _seed_expired_jobs(test_db_path)
    r = admin_client.post(
        "/api/admin/expirations/bulk-reactivate",
        data={
            "filters_json": json.dumps({"ats": ["greenhouse"]}),
            "confirm_count": 1,
            "csrf_token": "test-csrf",
        },
    )
    assert r.status_code == 400
    with get_db(test_db_path) as conn:
        assert int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE id IN (901, 902, 903) "
            "AND verification_status = 'expired'"
        ).fetchone()[0]) == 3


# ─── /api/admin/expirations/run-sweep ──────────────────────────────────────


def test_run_sweep_returns_result_dict(admin_client, test_db_path):
    r = admin_client.post(
        "/api/admin/expirations/run-sweep",
        data={"csrf_token": "test-csrf"},
    )
    # Either succeeds with a dict, or the verifier hit the network and
    # returned errors. We only assert the wrapper shape, not the network outcome.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "result" in body
