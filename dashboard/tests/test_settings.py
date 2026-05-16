from __future__ import annotations


def test_settings_info_shape(admin_client):
    res = admin_client.get("/api/admin/settings/")
    assert res.status_code == 200
    body = res.json()
    assert body["total_jobs"] == 6
    assert body["total_saved"] == 3
    assert isinstance(body["db_size_bytes"], int)
    assert isinstance(body["cache_size_bytes"], int)
    assert isinstance(body["manifest_url"], str)
    assert isinstance(body["debug"], bool)


def test_settings_by_ats_and_ingest_log(admin_client):
    by_ats = admin_client.get("/api/admin/settings/by_ats")
    assert by_ats.status_code == 200
    payload = by_ats.json()
    assert len(payload) >= 1
    assert payload[0]["count"] >= 1

    ingest_log = admin_client.get("/api/admin/settings/ingest_log?limit=2")
    assert ingest_log.status_code == 200
    rows = ingest_log.json()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed"

    bad = admin_client.get("/api/admin/settings/ingest_log?limit=999")
    assert bad.status_code == 422


def test_settings_api_blocks_non_admin(client, anon_client):
    res = client.get("/api/admin/settings/")
    assert res.status_code in (401, 403)

    anon = anon_client.get("/api/admin/settings/")
    assert anon.status_code == 401


def test_legacy_settings_path_works_for_admin(admin_client):
    ok = admin_client.get("/api/settings/")
    assert ok.status_code == 200


def test_legacy_settings_path_blocks_anon(anon_client):
    blocked = anon_client.get("/api/settings/")
    assert blocked.status_code == 401
