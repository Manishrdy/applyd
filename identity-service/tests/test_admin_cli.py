"""CLI smoke for the identity-service `app.cli` module."""

from __future__ import annotations

from pathlib import Path

import pytest


def _bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import config
    from app.database import init_db

    db_path = tmp_path / "cli.db"
    monkeypatch.setattr(config.settings, "db_path", db_path)
    init_db()


def test_cli_init_db(tmp_path, monkeypatch, capsys):
    _bootstrap(tmp_path, monkeypatch)
    from app.cli import main

    monkeypatch.setattr("sys.argv", ["cli", "init-db"])
    assert main() == 0
    assert "schema initialised" in capsys.readouterr().out


def test_cli_set_role_promotes_user(tmp_path, monkeypatch, capsys):
    _bootstrap(tmp_path, monkeypatch)
    from app.auth import create_user, get_user_role
    from app.cli import main

    create_user(name="X", email="who@test", password="Goodpass123!")
    monkeypatch.setattr("sys.argv", ["cli", "set-role", "who@test", "admin"])
    assert main() == 0
    captured = capsys.readouterr().out
    assert "role 'user' -> 'admin'" in captured

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email='who@test'").fetchone()
    assert get_user_role(int(row["id"])) == "admin"


def test_cli_set_role_unknown_email(tmp_path, monkeypatch, capsys):
    _bootstrap(tmp_path, monkeypatch)
    from app.cli import main

    monkeypatch.setattr("sys.argv", ["cli", "set-role", "nobody@test", "admin"])
    assert main() == 1
    err = capsys.readouterr().err
    assert "no such user" in err


def test_cli_set_role_invalid_role(tmp_path, monkeypatch, capsys):
    _bootstrap(tmp_path, monkeypatch)
    from app.cli import main

    # argparse choices=['user','admin'] will reject "wizard" before our code,
    # producing a SystemExit(2). Match the behaviour.
    monkeypatch.setattr("sys.argv", ["cli", "set-role", "anyone@test", "wizard"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_cli_list_users(tmp_path, monkeypatch, capsys):
    _bootstrap(tmp_path, monkeypatch)
    from app.auth import create_user
    from app.cli import main

    create_user(name="One", email="one@test", password="Goodpass123!")
    create_user(name="Two", email="two@test", password="Goodpass123!", role="admin")
    monkeypatch.setattr("sys.argv", ["cli", "list-users"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "one@test" in output
    assert "two@test" in output
    assert "admin" in output
