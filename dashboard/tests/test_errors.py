from __future__ import annotations

from fastapi import APIRouter


def test_api_404_is_json(client):
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404
    assert res.headers["content-type"].startswith("application/json")
    assert "detail" in res.json()


def test_html_404_is_templated(client):
    res = client.get("/does-not-exist")
    assert res.status_code == 404
    assert "text/html" in res.headers["content-type"]


def test_unhandled_exception_api_vs_html(client):
    from app.main import app

    router = APIRouter()

    @router.get("/api/_boom")
    def api_boom():
        raise RuntimeError("boom")

    @router.get("/_boom")
    def html_boom():
        raise RuntimeError("boom")

    app.include_router(router)

    api_res = client.get("/api/_boom")
    assert api_res.status_code == 500
    assert api_res.json()["detail"] == "internal server error"

    html_res = client.get("/_boom")
    assert html_res.status_code == 500
    assert "text/html" in html_res.headers["content-type"]
