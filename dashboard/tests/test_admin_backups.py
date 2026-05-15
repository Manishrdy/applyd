"""Tests for app.admin.services.backups — the service layer only.

Router-level tests (token gating, restore confirmation flow, etc.) come
in the per-feature suite once the templates / handlers are exercised.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.admin.services import backups as backup_service
from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_backup_root(tmp_path, monkeypatch):
    """Redirect the BACKUP_ROOT and identity DB path to tmp_path."""
    root = tmp_path / "backups"
    monkeypatch.setattr(backup_service, "BACKUP_ROOT", root)
    monkeypatch.setattr(backup_service, "_DASHBOARD_DIR", root / "dashboard")
    monkeypatch.setattr(backup_service, "_IDENTITY_DIR", root / "identity")

    # Give the "identity" source a real but tiny SQLite to back up.
    identity_db = tmp_path / "identity.db"
    conn = sqlite3.connect(str(identity_db))
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.execute("INSERT INTO t VALUES (1), (2), (3)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup_service, "_IDENTITY_DB_PATH", identity_db)
    yield


# ---- creation --------------------------------------------------------------


def test_create_backup_atomic_and_readable(test_db_path):
    created = backup_service.create_backup("dashboard")
    assert created.source == "dashboard"
    assert created.size_bytes > 0

    path = backup_service.resolve_for_download("dashboard", created.filename)
    conn = sqlite3.connect(str(path))
    row = conn.execute("PRAGMA integrity_check").fetchone()
    conn.close()
    assert str(row[0]).lower() == "ok"


def test_create_backup_identity_source(test_db_path):
    created = backup_service.create_backup("identity")
    assert created.source == "identity"
    # Must contain the row we inserted in the fixture.
    path = backup_service.resolve_for_download("identity", created.filename)
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT n FROM t ORDER BY n").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1, 2, 3]


def test_create_backup_missing_source_raises(monkeypatch, test_db_path):
    monkeypatch.setattr(settings, "db_path", Path("/nonexistent/path/applyd.db"))
    with pytest.raises(FileNotFoundError):
        backup_service.create_backup("dashboard")


# ---- listing ---------------------------------------------------------------


def test_list_backups_includes_both_sources(test_db_path):
    a = backup_service.create_backup("dashboard")
    b = backup_service.create_backup("identity")
    files = backup_service.list_backups()
    names = {(f.source, f.filename) for f in files}
    assert ("dashboard", a.filename) in names
    assert ("identity", b.filename) in names


# ---- filename safety -------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape.db",
        "..\\escape.db",
        "no-extension",
        "control\nchar.db",
        "spaces in name.db",
        "/abs/path.db",
    ],
)
def test_filename_whitelist_blocks_bad_paths(bad_name, test_db_path):
    with pytest.raises(ValueError):
        backup_service.resolve_for_download("dashboard", bad_name)


def test_delete_unknown_filename_raises(test_db_path):
    with pytest.raises(FileNotFoundError):
        backup_service.delete_backup("dashboard", "no_such_file.db")


def test_delete_round_trip(test_db_path):
    created = backup_service.create_backup("dashboard")
    backup_service.delete_backup("dashboard", created.filename)
    assert not any(f.filename == created.filename for f in backup_service.list_backups())


# ---- token verify ----------------------------------------------------------


def test_token_unconfigured_rejects_anything(monkeypatch, test_db_path):
    monkeypatch.delenv("APPLYD_BACKUP_TOKEN", raising=False)
    assert backup_service.is_token_configured() is False
    assert backup_service.verify_token("anything") is False
    assert backup_service.verify_token("") is False


def test_token_configured_matches(monkeypatch, test_db_path):
    monkeypatch.setenv("APPLYD_BACKUP_TOKEN", "secret-xyz")
    assert backup_service.is_token_configured() is True
    assert backup_service.verify_token("secret-xyz") is True
    assert backup_service.verify_token("nope") is False
    assert backup_service.verify_token("") is False


# ---- restore safety -------------------------------------------------------


def test_restore_takes_pre_snapshot_and_swaps(test_db_path, monkeypatch, tmp_path):
    # Point the dashboard source at a writable test DB so restore can swap.
    live = tmp_path / "live.db"
    seed = sqlite3.connect(str(live))
    seed.execute("CREATE TABLE state (val TEXT)")
    seed.execute("INSERT INTO state VALUES ('before')")
    seed.commit()
    seed.close()
    monkeypatch.setattr(settings, "db_path", live)

    # Take a backup of the "before" state.
    snapshot = backup_service.create_backup("dashboard")

    # Mutate live.
    mut = sqlite3.connect(str(live))
    mut.execute("UPDATE state SET val='after'")
    mut.commit()
    mut.close()

    # Restore the earlier snapshot.
    result = backup_service.restore_from("dashboard", snapshot.filename)
    assert result.pre_restore_snapshot.startswith("pre_restore_")
    assert result.bytes_restored > 0

    # Live should reflect "before" again.
    conn = sqlite3.connect(str(live))
    val = conn.execute("SELECT val FROM state").fetchone()[0]
    conn.close()
    assert val == "before"

    # The safety snapshot should be on disk and itself a valid DB.
    safety_path = backup_service.resolve_for_download("dashboard", result.pre_restore_snapshot)
    s = sqlite3.connect(str(safety_path))
    sval = s.execute("SELECT val FROM state").fetchone()[0]
    s.close()
    assert sval == "after"


def test_restore_rejects_corrupt_backup(test_db_path, monkeypatch, tmp_path):
    live = tmp_path / "live.db"
    live.write_bytes(b"")  # empty placeholder
    monkeypatch.setattr(settings, "db_path", live)

    bad = backup_service._DASHBOARD_DIR / "broken.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a sqlite file at all")

    with pytest.raises(Exception):
        backup_service.restore_from("dashboard", "broken.db")
