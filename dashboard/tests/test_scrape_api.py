from __future__ import annotations

from app.database import get_db
from app.services import local_scraper


def test_scrape_presets_crud(client):
    payload = {
        "name": "Ashby Incremental",
        "ats_requested": ["ashby"],
        "max_companies_per_ats": 500,
        "incremental_enabled": True,
        "incremental_days": 7,
        "notes": "daily",
        "is_default": True,
    }
    r = client.post("/api/scrape/presets", json=payload)
    assert r.status_code == 200
    preset = r.json()["preset"]
    assert preset["name"] == payload["name"]
    pid = preset["id"]

    r = client.get("/api/scrape/presets")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json()["presets"])

    payload["name"] = "Ashby Incremental v2"
    r = client.put(f"/api/scrape/presets/{pid}", json=payload)
    assert r.status_code == 200
    assert r.json()["preset"]["name"] == "Ashby Incremental v2"

    r = client.delete(f"/api/scrape/presets/{pid}")
    assert r.status_code == 204


def test_coverage_summary_and_detail(client, monkeypatch):
    monkeypatch.setattr(local_scraper, "available_ats", lambda: ["ashby"])
    monkeypatch.setattr(
        local_scraper,
        "read_company_catalog",
        lambda _ats: [
            local_scraper.CompanyRef("A", "a", ""),
            local_scraper.CompanyRef("B", "b", ""),
            local_scraper.CompanyRef("C", "c", ""),
        ],
    )

    with get_db() as conn:
        conn.execute(
            "INSERT INTO scrape_company_state(ats, slug, last_scraped_at, last_status, success_count, failure_count) "
            "VALUES ('ashby', 'a', datetime('now'), 'succeeded', 1, 0)"
        )
        conn.execute(
            "INSERT INTO scrape_company_state(ats, slug, last_scraped_at, last_status, success_count, failure_count) "
            "VALUES ('ashby', 'b', datetime('now', '-10 day'), 'failed', 2, 1)"
        )

    r = client.get("/api/scrape/coverage?ats=ashby")
    assert r.status_code == 200
    cov = r.json()["coverage"]["ashby"]
    assert cov["never"] == 1
    assert cov["0_1d"] == 1
    assert cov["8_30d"] == 1

    r = client.get("/api/scrape/coverage/ashby")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 3
    assert {row["slug"] for row in rows} == {"a", "b", "c"}
