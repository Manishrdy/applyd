from __future__ import annotations


def test_all_api_endpoints_have_basic_coverage_smoke(client, monkeypatch):
    from app import main

    async def _fake_ingest(*args, **kwargs):
        return {"status": "success", "rows_ingested": 0}

    monkeypatch.setattr(main, "run_ingestion", _fake_ingest)

    endpoints = [
        ("GET", "/api/health"),
        ("POST", "/api/ingest"),
        ("GET", "/api/ingest/status"),
        ("GET", "/api/jobs/"),
        ("GET", "/api/jobs/facets"),
        ("GET", "/api/jobs/companies"),
        ("GET", "/api/jobs/export"),
        ("GET", "/api/jobs/1"),
        ("GET", "/api/saved/"),
        ("POST", "/api/saved/1"),
        ("PATCH", "/api/saved/1"),
        ("DELETE", "/api/saved/1"),
        ("GET", "/api/stats/summary"),
        ("GET", "/api/stats/by_ats"),
        ("GET", "/api/stats/by_day"),
        ("GET", "/api/stats/by_country"),
        ("GET", "/api/stats/top_companies"),
        ("GET", "/api/stats/salary_range"),
        ("GET", "/api/stats/remote_vs_onsite"),
        ("GET", "/api/settings/"),
        ("GET", "/api/settings/by_ats"),
        ("GET", "/api/settings/ingest_log"),
    ]

    for method, path in endpoints:
        if method == "GET":
            res = client.get(path)
        elif method == "POST":
            res = client.post(path, json={})
        elif method == "PATCH":
            res = client.patch(path, json={"status": "queued"})
        else:
            res = client.delete(path)
        assert res.status_code < 500 or path in {"/api/ingest"}
