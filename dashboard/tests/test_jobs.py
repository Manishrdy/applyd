from __future__ import annotations

import csv
import io


def _job_ids(body: dict) -> list[int]:
    return [j["id"] for j in body["jobs"]]


def test_jobs_list_filters_and_pagination(client):
    res = client.get("/api/jobs/?country=US&posted_hours=0&limit=2&page=1&sort=posted_at_desc")
    assert res.status_code == 200
    body = res.json()
    assert body["limit"] == 2
    assert body["page"] == 1
    assert body["sort"] == "newest"
    assert body["has_more"] is True
    assert body["total"] == 3
    assert all(job["country"] == "US" for job in body["jobs"])


def test_jobs_list_sort_and_page_overflow(client):
    newest = client.get("/api/jobs/?country=US&posted_hours=0&sort=newest")
    oldest = client.get("/api/jobs/?country=US&posted_hours=0&sort=oldest")
    high = client.get("/api/jobs/?country=US&posted_hours=0&sort=salary_high")
    low = client.get("/api/jobs/?country=US&posted_hours=0&sort=salary_low")

    assert newest.status_code == oldest.status_code == high.status_code == low.status_code == 200
    assert _job_ids(newest.json()) == [1, 2, 4]
    assert _job_ids(oldest.json()) == [4, 2, 1]
    assert _job_ids(high.json()) == [1, 2, 4]
    assert _job_ids(low.json()) == [4, 2, 1]

    overflow = client.get("/api/jobs/?country=US&posted_hours=0&limit=2&page=3")
    assert overflow.status_code == 200
    assert overflow.json()["jobs"] == []
    assert overflow.json()["has_more"] is False


def test_jobs_list_filter_matrix_and_include_undated(client):
    remote = client.get("/api/jobs/?country=US&posted_hours=0&remote=true")
    assert remote.status_code == 200
    assert _job_ids(remote.json()) == [1, 4]

    dept = client.get("/api/jobs/?country=US&posted_hours=0&department=Platform")
    assert dept.status_code == 200
    assert _job_ids(dept.json()) == [1]

    ats = client.get("/api/jobs/?country=US&posted_hours=0&ats=lever")
    assert ats.status_code == 200
    assert _job_ids(ats.json()) == [2]

    salary = client.get("/api/jobs/?country=US&posted_hours=0&salary_min_usd=150000")
    assert salary.status_code == 200
    assert _job_ids(salary.json()) == [1]

    company = client.get("/api/jobs/?posted_hours=0&company=Acme")
    assert company.status_code == 200
    assert set(_job_ids(company.json())) == {1, 4}

    undated_default = client.get("/api/jobs/?country=CA&posted_hours=0")
    undated_off = client.get("/api/jobs/?country=CA&posted_hours=0&include_undated=false")
    assert undated_default.status_code == undated_off.status_code == 200
    assert _job_ids(undated_default.json()) == [3]
    assert _job_ids(undated_off.json()) == []


def test_jobs_list_fts_and_relevance_sort(client):
    res = client.get("/api/jobs/?q=Backend&posted_hours=0&sort=relevance")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert _job_ids(body) == [1]


def test_jobs_list_validation_422(client):
    assert client.get("/api/jobs/?posted_hours=999").status_code == 422
    assert client.get("/api/jobs/?limit=0").status_code == 422
    assert client.get("/api/jobs/?page=0").status_code == 422


def test_jobs_facets_and_selection(client):
    res = client.get("/api/jobs/facets?facets=country&facets=remote&country=US&posted_hours=0")
    assert res.status_code == 200
    body = res.json()
    names = [f["name"] for f in body["facets"]]
    assert names == ["country", "remote"]
    assert body["total_matching"] == 3

    remote_counts = next(f["counts"] for f in body["facets"] if f["name"] == "remote")
    by_val = {str(item["value"]): item["count"] for item in remote_counts}
    assert by_val["True"] == 2
    assert by_val["False"] == 1


def test_jobs_companies_and_export(client):
    companies = client.get("/api/jobs/companies?posted_hours=0&limit=10")
    assert companies.status_code == 200
    payload = companies.json()["companies"]
    assert payload[0]["company"] == "Acme"
    assert payload[0]["count"] == 2

    export = client.get("/api/jobs/export?country=US&max_rows=3&posted_hours=0&sort=oldest")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["content-disposition"] == "attachment; filename=\"applyd-jobs.csv\""

    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 3
    assert [int(r["id"]) for r in rows] == [4, 2, 1]


def test_jobs_export_validation_bounds(client):
    assert client.get("/api/jobs/export?max_rows=0").status_code == 422
    assert client.get("/api/jobs/export?max_rows=10001").status_code == 422


def test_jobs_detail_found_and_404(client):
    ok = client.get("/api/jobs/1")
    assert ok.status_code == 200
    body = ok.json()
    assert body["id"] == 1
    assert body["is_saved"] is True

    miss = client.get("/api/jobs/99999")
    assert miss.status_code == 404
    assert miss.json()["detail"] == "job not found"
