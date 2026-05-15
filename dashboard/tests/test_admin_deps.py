"""Unit tests for app.admin.deps.

Bypasses the middleware so we can inject `request.state` directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.admin.deps import AdminUser, get_current_user, require_admin_user


def _request(*, user_id=None, email=None, role=None):
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id, user_email=email, user_role=role))


def test_get_current_user_returns_state():
    req = _request(user_id=7, email="a@test", role="admin")
    user = get_current_user(req)
    assert isinstance(user, AdminUser)
    assert user.id == 7
    assert user.email == "a@test"
    assert user.role == "admin"


def test_get_current_user_defaults_role_when_missing():
    req = _request(user_id=1, email=None, role=None)
    user = get_current_user(req)
    assert user.role == "user"
    assert user.email == ""


def test_get_current_user_no_user_id_is_401():
    req = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        get_current_user(req)
    assert exc.value.status_code == 401


def test_require_admin_user_allows_admin():
    req = _request(user_id=7, email="a@test", role="admin")
    user = require_admin_user(req)
    assert user.role == "admin"


def test_require_admin_user_blocks_user():
    req = _request(user_id=2, email="u@test", role="user")
    with pytest.raises(HTTPException) as exc:
        require_admin_user(req)
    assert exc.value.status_code == 403


def test_require_admin_user_blocks_unauthenticated():
    req = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        require_admin_user(req)
    # get_current_user runs first and returns 401 before the role check.
    assert exc.value.status_code == 401
