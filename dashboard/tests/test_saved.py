from __future__ import annotations


def test_saved_list_and_invalid_status(client):
    all_saved = client.get("/api/saved/")
    assert all_saved.status_code == 200
    assert all_saved.json()["total"] == 3

    queued = client.get("/api/saved/?status=queued")
    assert queued.status_code == 200
    body = queued.json()
    assert body["total"] == 1
    assert all(item["status"] == "queued" for item in body["saved"])

    bad = client.get("/api/saved/?status=wrong")
    assert bad.status_code == 400


def test_saved_post_patch_delete_and_errors(client):
    create = client.post("/api/saved/4", json={"notes": "new", "status": "queued"})
    assert create.status_code == 200
    assert create.json() == {"saved": True, "job_id": 4}

    upsert = client.post("/api/saved/4", json={"status": "applied"})
    assert upsert.status_code == 200

    check = client.get("/api/saved/?status=applied")
    assert check.status_code == 200
    assert any(item["id"] == 4 for item in check.json()["saved"])

    notes_only = client.patch("/api/saved/4", json={"notes": "updated"})
    assert notes_only.status_code == 200

    status_only = client.patch("/api/saved/4", json={"status": "archived"})
    assert status_only.status_code == 200

    verify = client.get("/api/saved/?status=archived")
    assert verify.status_code == 200
    archived = [item for item in verify.json()["saved"] if item["id"] == 4]
    assert len(archived) == 1
    assert archived[0]["notes"] == "updated"

    empty_patch = client.patch("/api/saved/4", json={})
    assert empty_patch.status_code == 200

    bad_status_post = client.post("/api/saved/4", json={"status": "invalid"})
    assert bad_status_post.status_code == 400

    bad_status_patch = client.patch("/api/saved/4", json={"status": "invalid"})
    assert bad_status_patch.status_code == 400

    bad_patch = client.patch("/api/saved/5", json={"status": "queued"})
    assert bad_patch.status_code == 404

    bad_create = client.post("/api/saved/9999", json={"status": "queued"})
    assert bad_create.status_code == 404

    delete = client.delete("/api/saved/4")
    assert delete.status_code == 200
    assert delete.json()["saved"] is False


def test_saved_isolation_between_users(client, client_b):
    # Seed: user 1 has 3 saves; user 2 has 0.
    assert client.get("/api/saved/").json()["total"] == 3
    assert client_b.get("/api/saved/").json()["total"] == 0

    # User B saves job 1. A still 3, B = 1.
    assert client_b.post("/api/saved/1").status_code == 200
    assert client.get("/api/saved/").json()["total"] == 3
    assert client_b.get("/api/saved/").json()["total"] == 1

    # User B unsave does not affect user A's row for job 1.
    assert client_b.delete("/api/saved/1").status_code == 200
    assert client.get("/api/saved/").json()["total"] == 3
    assert client_b.get("/api/saved/").json()["total"] == 0


def test_is_saved_is_per_user_on_jobs_list(client, client_b):
    rows_a = client.get("/api/jobs/?posted_hours=720").json()["jobs"]
    job_a = next(j for j in rows_a if j["id"] == 1)
    assert job_a["is_saved"] is True

    rows_b = client_b.get("/api/jobs/?posted_hours=720").json()["jobs"]
    job_b = next(j for j in rows_b if j["id"] == 1)
    assert job_b["is_saved"] is False


def test_is_saved_per_user_on_jobs_detail(client, client_b):
    assert client.get("/api/jobs/1").json()["is_saved"] is True
    assert client_b.get("/api/jobs/1").json()["is_saved"] is False


def test_patch_404_when_other_user_has_it(client_b):
    # User B has not saved job 1; PATCH 404s even though user A has.
    r = client_b.patch("/api/saved/1", json={"status": "applied"})
    assert r.status_code == 404


def test_delete_is_silent_noop_for_other_user(client, client_b):
    # B's DELETE returns 200 idempotently; A's save stays intact.
    r = client_b.delete("/api/saved/1")
    assert r.status_code == 200
    assert client.get("/api/saved/").json()["total"] == 3


def test_saved_requires_auth(anon_client):
    assert anon_client.get("/api/saved/").status_code == 401
    assert anon_client.post("/api/saved/1").status_code == 401
    assert anon_client.delete("/api/saved/1").status_code == 401
    assert anon_client.patch("/api/saved/1", json={"status": "queued"}).status_code == 401
