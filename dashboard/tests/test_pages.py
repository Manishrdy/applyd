from __future__ import annotations


HTML_ROUTES = [
    "/",
    "/placeholder",
    "/styleguide",
    "/saved",
    "/stats",
    "/settings",
]


def test_html_pages_render(client):
    for path in HTML_ROUTES:
        res = client.get(path)
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]


def test_html_page_markers(client):
    home = client.get("/")
    assert "jobs" in home.text.lower()

    placeholder = client.get("/placeholder")
    assert "applyd" in placeholder.text.lower()

    styleguide = client.get("/styleguide")
    assert "style" in styleguide.text.lower()


def test_job_page_found_and_404(client):
    ok = client.get("/job/1")
    assert ok.status_code == 200
    assert "Backend Engineer" in ok.text

    miss = client.get("/job/9999")
    assert miss.status_code == 404
    assert "text/html" in miss.headers["content-type"]
