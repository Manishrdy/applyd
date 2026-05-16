from __future__ import annotations


def test_my_stats_per_user(client, client_b):
    a = client.get("/api/me/stats")
    assert a.status_code == 200
    body = a.json()

    assert body["total_saved"] == 3
    assert body["by_status"] == {
        "queued": 1, "applied": 1, "skipped": 1, "archived": 0
    }
    # 1 applied / 3 total = 0.333…
    assert 0.32 < body["conversion_rate"] < 0.34

    company_keys = {row["key"] for row in body["top_companies"]}
    assert company_keys == {"Acme", "Beta", "Gamma"}

    ats_keys = {row["key"] for row in body["top_ats"]}
    assert ats_keys == {"greenhouse", "lever", "workday"}

    # Shape-only: 30 points each with date+count. Dates are absolute
    # (today minus 0..29) so specific values aren't stable across runs.
    assert len(body["saves_per_day"]) == 30
    assert all("date" in p and "count" in p for p in body["saves_per_day"])

    # User B has no saves — everything zero.
    b = client_b.get("/api/me/stats").json()
    assert b["total_saved"] == 0
    assert b["by_status"] == {"queued": 0, "applied": 0, "skipped": 0, "archived": 0}
    assert b["conversion_rate"] == 0.0
    assert b["top_companies"] == []
    assert b["top_ats"] == []


def test_my_stats_requires_auth(anon_client):
    assert anon_client.get("/api/me/stats").status_code == 401


def test_my_stats_reflects_new_save(client_b):
    # B starts empty; after saving job 1, /me/stats reflects it.
    assert client_b.post("/api/saved/1").status_code == 200
    body = client_b.get("/api/me/stats").json()
    assert body["total_saved"] == 1
    assert body["by_status"]["queued"] == 1
    assert body["conversion_rate"] == 0.0
    assert {row["key"] for row in body["top_companies"]} == {"Acme"}
