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
