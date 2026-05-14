from __future__ import annotations


def test_settings_info_shape(client):
    res = client.get("/api/settings/")
    assert res.status_code == 200
    body = res.json()
    assert body["total_jobs"] == 6
    assert body["total_saved"] == 3
    assert isinstance(body["db_size_bytes"], int)
    assert isinstance(body["cache_size_bytes"], int)
    assert isinstance(body["manifest_url"], str)
    assert isinstance(body["debug"], bool)


def test_settings_by_ats_and_ingest_log(client):
    by_ats = client.get("/api/settings/by_ats")
    assert by_ats.status_code == 200
    payload = by_ats.json()
    assert len(payload) >= 1
    assert payload[0]["count"] >= 1

    ingest_log = client.get("/api/settings/ingest_log?limit=2")
    assert ingest_log.status_code == 200
    rows = ingest_log.json()
    assert len(rows) == 2
    assert rows[0]["status"] == "failed"

    bad = client.get("/api/settings/ingest_log?limit=999")
    assert bad.status_code == 422
