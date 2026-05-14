from __future__ import annotations


def test_health_shape(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["total_jobs"] == 6
    assert body["rolling_window_days"] == 30
    assert body["last_ingest"]["status"] == "success"


def test_ingest_status_limit_bounds(client):
    min_limit = client.get("/api/ingest/status?limit=0")
    assert min_limit.status_code == 200
    assert len(min_limit.json()["recent"]) == 1

    max_limit = client.get("/api/ingest/status?limit=1000")
    assert max_limit.status_code == 200
    assert len(max_limit.json()["recent"]) == 2


def test_ingest_error_mapping(client, monkeypatch):
    from app import main

    async def boom(force: bool = False):
        raise RuntimeError("fail")

    monkeypatch.setattr(main, "run_ingestion", boom)

    res = client.post("/api/ingest")
    assert res.status_code == 500
    assert "fail" in res.json()["detail"]
