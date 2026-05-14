from __future__ import annotations


def _to_map(items: list[dict]) -> dict[str, int]:
    return {i["label"]: i["count"] for i in items}


def test_stats_summary_and_validation(client):
    summary = client.get("/api/stats/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["total_jobs"] == 6
    assert body["dated"] == 5
    assert body["undated"] == 1
    assert body["us_total"] == 4
    assert body["remote"] == 3
    assert body["ats_count"] == 4
    assert body["company_count"] == 5

    assert client.get("/api/stats/by_ats?days=99").status_code == 422
    assert client.get("/api/stats/by_day?days=0").status_code == 422


def test_stats_grouped_endpoints_with_expected_counts(client):
    by_ats = client.get("/api/stats/by_ats?days=30&limit=10")
    assert by_ats.status_code == 200
    ats = _to_map(by_ats.json()["items"])
    assert ats["greenhouse"] == 2
    assert ats["lever"] == 1
    assert ats["workday"] == 1
    assert ats["icims"] == 1

    by_country = client.get("/api/stats/by_country?days=30&limit=10")
    assert by_country.status_code == 200
    countries = _to_map(by_country.json()["items"])
    assert countries["US"] == 3
    assert countries["CA"] == 1
    assert countries["GB"] == 1

    by_day = client.get("/api/stats/by_day?country=US&days=30")
    assert by_day.status_code == 200
    days = _to_map(by_day.json()["items"])
    assert days["2026-05-01"] == 1
    assert days["2026-05-10"] == 1
    assert days["2026-05-12"] == 1

    top = client.get("/api/stats/top_companies?days=30&country=US&limit=10")
    assert top.status_code == 200
    items = top.json()["items"]
    assert items[0]["label"] == "Acme"
    assert items[0]["count"] == 2


def test_stats_salary_and_remote(client):
    salary = client.get("/api/stats/salary_range?country=US&days=30")
    assert salary.status_code == 200
    sal = salary.json()
    assert len(sal["buckets"]) == 8
    assert sum(bucket["count"] for bucket in sal["buckets"]) == 3
    assert sal["median"] == 120000
    assert sal["p25"] == 95000
    assert sal["p75"] == 160000

    remote = client.get("/api/stats/remote_vs_onsite?country=US&days=30")
    assert remote.status_code == 200
    assert remote.json() == {"remote": 2, "onsite": 1, "unknown": 0}


def test_stats_future_rows_excluded_from_time_windows(client):
    wide = client.get("/api/stats/by_country?days=0&limit=10")
    assert wide.status_code == 200
    countries = _to_map(wide.json()["items"])
    assert countries["US"] == 3
