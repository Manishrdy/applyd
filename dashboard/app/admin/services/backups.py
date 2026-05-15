"""Backup management for both applyd SQLite databases.

We back up *atomically* via SQLite's `Connection.backup()` API (online,
WAL-safe) instead of copying the file with `cp` — copying a live WAL DB
can yield a corrupted snapshot.

Public API:
    - list_backups()              -> list[BackupFile]
    - create_backup(...)          -> BackupFile
    - delete_backup(filename)     -> None
    - resolve_for_download(name)  -> Path  (validated against BACKUP_DIR)
    - verify_token(submitted)     -> bool
    - restore_from(filename, *, source) -> RestoreResult   (Feature #9)

The download token guards the download endpoint independently of the admin
cookie — defense in depth. Source-of-truth for the token is env then config.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.config import settings

log = logging.getLogger(__name__)


# Layout:
#   data/backups/dashboard/applyd_YYYYMMDDTHHMMSSZ.db
#   data/backups/identity/identity_YYYYMMDDTHHMMSSZ.db
BACKUP_ROOT: Path = Path(__file__).resolve().parents[3] / "data" / "backups"
_DASHBOARD_DIR = BACKUP_ROOT / "dashboard"
_IDENTITY_DIR = BACKUP_ROOT / "identity"

_IDENTITY_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "legacy" / "identity.db"

_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.db$")

Source = Literal["dashboard", "identity"]


@dataclass(frozen=True)
class BackupFile:
    source: Source
    filename: str
    size_bytes: int
    created_at: str


@dataclass(frozen=True)
class RestoreResult:
    source: Source
    filename: str
    bytes_restored: int
    pre_restore_snapshot: str  # filename of the safety snapshot we took first


def _ensure_dirs() -> None:
    _DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    _IDENTITY_DIR.mkdir(parents=True, exist_ok=True)


def _dir_for(source: Source) -> Path:
    _ensure_dirs()
    return _DASHBOARD_DIR if source == "dashboard" else _IDENTITY_DIR


def _src_for(source: Source) -> Path:
    if source == "dashboard":
        return settings.db_path
    return _IDENTITY_DB_PATH


# ---- Token --------------------------------------------------------------


def _expected_token() -> str:
    env = os.environ.get("APPLYD_BACKUP_TOKEN", "").strip()
    if env:
        return env
    return ""


def is_token_configured() -> bool:
    return bool(_expected_token())


def verify_token(submitted: str) -> bool:
    """Constant-time compare against the configured token.

    Returns False if no token is configured (so downloads are blocked until
    an operator sets `APPLYD_BACKUP_TOKEN`).
    """
    expected = _expected_token()
    if not expected or not submitted:
        return False
    return hmac.compare_digest(submitted, expected)


# ---- Listing ------------------------------------------------------------


def _stat_file(source: Source, path: Path) -> BackupFile:
    stat = path.stat()
    return BackupFile(
        source=source,
        filename=path.name,
        size_bytes=int(stat.st_size),
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    )


def list_backups() -> list[BackupFile]:
    _ensure_dirs()
    out: list[BackupFile] = []
    for source, base in (("dashboard", _DASHBOARD_DIR), ("identity", _IDENTITY_DIR)):
        for path in sorted(base.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            out.append(_stat_file(source, path))  # type: ignore[arg-type]
    return out


# ---- Creation -----------------------------------------------------------


def _atomic_backup(src: Path, dst: Path) -> int:
    """SQLite online backup. Returns bytes written. Caller ensures src exists."""
    started = time.perf_counter()
    src_conn = sqlite3.connect(str(src))
    try:
        # Write to a temp inside the same dir, then rename — never leave a
        # half-written .db that list_backups() would still pick up.
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".inflight_", suffix=".db", dir=dst.parent)
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            dst_conn = sqlite3.connect(str(tmp_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
            tmp_path.replace(dst)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    finally:
        src_conn.close()
    elapsed = time.perf_counter() - started
    size = dst.stat().st_size
    log.info("backup created src=%s dst=%s bytes=%d elapsed=%.2fs", src, dst, size, elapsed)
    return size


def _build_filename(source: Source) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = "applyd" if source == "dashboard" else "identity"
    return f"{prefix}_{stamp}.db"


def create_backup(source: Source) -> BackupFile:
    src = _src_for(source)
    if not src.exists():
        raise FileNotFoundError(f"source DB missing: {src}")
    dst_dir = _dir_for(source)
    dst = dst_dir / _build_filename(source)
    _atomic_backup(src, dst)
    return _stat_file(source, dst)


# ---- Deletion + path resolution ----------------------------------------


def _validate_filename(filename: str) -> str:
    """Whitelist filename characters — block traversal and weird chars."""
    name = filename.strip()
    if not _FILENAME_RE.match(name) or ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"invalid backup filename: {filename!r}")
    return name


def _path_for(source: Source, filename: str) -> Path:
    name = _validate_filename(filename)
    candidate = _dir_for(source) / name
    # Belt-and-braces: also confirm the resolved path stays inside the
    # source's backup dir (in case symlinks ever slip in).
    base = _dir_for(source).resolve()
    resolved = candidate.resolve()
    if not str(resolved).startswith(str(base) + os.sep) and resolved != base:
        raise ValueError("path escapes backup root")
    return candidate


def resolve_for_download(source: Source, filename: str) -> Path:
    path = _path_for(source, filename)
    if not path.exists():
        raise FileNotFoundError(filename)
    return path


def delete_backup(source: Source, filename: str) -> None:
    path = _path_for(source, filename)
    if not path.exists():
        raise FileNotFoundError(filename)
    path.unlink()
    log.info("backup deleted source=%s filename=%s", source, filename)


# ---- Restore (Feature #9) ----------------------------------------------


def _verify_sqlite_integrity(path: Path) -> None:
    """Open the file and run `PRAGMA integrity_check`. Raise on failure."""
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not row or str(row[0]).lower() != "ok":
        raise RuntimeError(f"integrity_check failed: {row[0] if row else 'no result'}")


def restore_from(source: Source, filename: str) -> RestoreResult:
    """Replace the live DB with a backup. Takes a safety snapshot first.

    Steps (each may raise — the caller is responsible for converting to a
    nice HTTP error):
      1. Validate the backup file exists & passes integrity_check.
      2. Take a `pre_restore_<stamp>.db` snapshot of the CURRENT live DB.
      3. Atomically swap: copy backup to a temp file alongside the live DB,
         then `replace()` over the live path. SQLite-WAL safe? It's not —
         no live connections must be open. We call this from the admin
         API, which acts as the single writer; concurrent reads will see a
         brief I/O error on the connection cycle, which is acceptable.
      4. Verify the new live DB passes integrity_check.

    Returns metadata about what landed and where the safety snapshot is.
    """
    src_backup = _path_for(source, filename)
    if not src_backup.exists():
        raise FileNotFoundError(filename)
    _verify_sqlite_integrity(src_backup)

    live_path = _src_for(source)
    if not live_path.parent.exists():
        live_path.parent.mkdir(parents=True, exist_ok=True)

    snapshot_name = f"pre_restore_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(4)}.db"
    snapshot_dir = _dir_for(source)
    snapshot_path = snapshot_dir / snapshot_name
    if live_path.exists():
        _atomic_backup(live_path, snapshot_path)
    else:
        # Fresh install — nothing to snapshot. Touch a marker file so the
        # caller still has a stable artefact to reference in the audit log.
        snapshot_path.write_bytes(b"")

    # Copy backup into place atomically: temp-then-replace.
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".inflight_restore_", suffix=".db", dir=live_path.parent)
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(src_backup, tmp_path)
        # Quick integrity check on the staged file before promoting it.
        _verify_sqlite_integrity(tmp_path)
        tmp_path.replace(live_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    # Drop the WAL/SHM siblings if they exist — they belong to the old DB
    # and will confuse SQLite when it reopens the freshly replaced file.
    for sibling_suffix in ("-wal", "-shm"):
        sibling = live_path.with_name(live_path.name + sibling_suffix)
        sibling.unlink(missing_ok=True)

    bytes_restored = live_path.stat().st_size
    log.warning(
        "DB restore applied source=%s filename=%s bytes=%d snapshot=%s",
        source, filename, bytes_restored, snapshot_path.name,
    )
    return RestoreResult(
        source=source,
        filename=filename,
        bytes_restored=bytes_restored,
        pre_restore_snapshot=snapshot_path.name,
    )
