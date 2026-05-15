from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main
    from app.database import init_db

    db_path = tmp_path / "identity-test.db"
    monkeypatch.setattr(main.settings, "db_path", db_path)
    monkeypatch.setattr(main.settings, "session_cookie_secure", False)
    monkeypatch.setattr(main.settings, "session_cookie_samesite", "lax")
    monkeypatch.setattr(main.settings, "session_cookie_domain", None)
    monkeypatch.setattr(main.settings, "session_cookie_max_age_seconds", None)
    monkeypatch.setattr(main.settings, "csrf_cookie_secure", False)
    monkeypatch.setattr(main.settings, "csrf_cookie_samesite", "lax")
    monkeypatch.setattr(main.settings, "auth_rate_limit_window_seconds", 300)
    monkeypatch.setattr(main.settings, "auth_rate_limit_max_attempts", 5)
    monkeypatch.setattr(main.settings, "auth_rate_limit_lockout_seconds", 600)
    monkeypatch.setattr(main.settings, "redirect_allow_hosts", "localhost:8000,127.0.0.1:8000")

    init_db()

    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client
