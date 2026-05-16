from __future__ import annotations

HTML_ROUTES = [
    "/dashboard",
    "/placeholder",
    "/styleguide",
    "/saved",
    "/stats",
]


def test_html_pages_render(client):
    for path in HTML_ROUTES:
        res = client.get(path)
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]


def test_html_page_markers(client, monkeypatch):
    from app.routers import pages as pages_router

    monkeypatch.setattr(
        pages_router,
        "verify_request_user",
        lambda request: {"authenticated": True, "user_id": 1, "email": "user@test", "role": "user"},
    )
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers.get("location") == "/dashboard"

    dashboard = client.get("/dashboard")
    assert "jobs" in dashboard.text.lower()

    styleguide = client.get("/styleguide")
    assert "style" in styleguide.text.lower()


def test_job_page_found_and_404(client):
    ok = client.get("/job/1")
    assert ok.status_code == 200
    assert "Backend Engineer" in ok.text

    miss = client.get("/job/9999")
    assert miss.status_code == 404
    assert "text/html" in miss.headers["content-type"]


def test_dashboard_redirects_when_logged_out(anon_client):
    home = anon_client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "Sign in" in home.text

    res = anon_client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/signin")
