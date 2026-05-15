"""Admin API for backup management + token-gated download + restore.

Endpoints:
    GET    /api/admin/backups                   list
    POST   /api/admin/backups                   create (source=dashboard|identity)
    POST   /api/admin/backups/{source}/{name}/delete
    POST   /api/admin/backups/{source}/{name}/download   token-gated (form POST)
    POST   /api/admin/backups/{source}/{name}/restore     token + double-confirm
"""

from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse

from app.admin import audit
from app.admin.deps import AdminUser, require_admin_user
from app.admin.services import backups as backup_service
from app.admin.services import maintenance as maintenance_service


router = APIRouter()


_VALID_SOURCES = ("dashboard", "identity")


def _check_source(source: str) -> None:
    if source not in _VALID_SOURCES:
        raise HTTPException(status_code=400, detail="invalid source")


@router.get("/backups")
def list_backups(admin: AdminUser = Depends(require_admin_user)):
    files = [asdict(b) for b in backup_service.list_backups()]
    return {
        "files": files,
        "token_configured": backup_service.is_token_configured(),
    }


@router.post("/backups")
def create_backup(
    request: Request,
    source: str = Form(...),
    admin: AdminUser = Depends(require_admin_user),
):
    _check_source(source)
    try:
        created = backup_service.create_backup(source)  # type: ignore[arg-type]
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit.record(
        admin=admin,
        action="create_backup",
        target=f"{source}/{created.filename}",
        detail={"size_bytes": created.size_bytes},
        request=request,
    )
    return asdict(created)


@router.post("/backups/{source}/{filename}/delete")
def delete_backup(
    source: str,
    filename: str,
    request: Request,
    admin: AdminUser = Depends(require_admin_user),
):
    _check_source(source)
    try:
        backup_service.delete_backup(source, filename)  # type: ignore[arg-type]
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="backup not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit.record(
        admin=admin,
        action="delete_backup",
        target=f"{source}/{filename}",
        request=request,
    )
    return {"deleted": True, "source": source, "filename": filename}


@router.post("/backups/{source}/{filename}/download")
def download_backup(
    source: str,
    filename: str,
    request: Request,
    token: str = Form(""),
    admin: AdminUser = Depends(require_admin_user),
):
    """Token-gated download. The token is a separate secret from the cookie.

    POST (not GET) so the token doesn't end up in browser history / referer.
    """
    _check_source(source)
    if not backup_service.is_token_configured():
        raise HTTPException(status_code=503, detail="backup token not configured (set APPLYD_BACKUP_TOKEN)")
    if not backup_service.verify_token(token):
        audit.record(
            admin=admin,
            action="download_backup_denied",
            target=f"{source}/{filename}",
            detail="bad_token",
            request=request,
        )
        raise HTTPException(status_code=403, detail="invalid backup token")
    try:
        path = backup_service.resolve_for_download(source, filename)  # type: ignore[arg-type]
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    audit.record(
        admin=admin,
        action="download_backup",
        target=f"{source}/{filename}",
        request=request,
    )
    return FileResponse(
        path=str(path),
        filename=filename,
        media_type="application/x-sqlite3",
    )


@router.post("/backups/{source}/{filename}/restore")
def restore_backup(
    source: str,
    filename: str,
    request: Request,
    token: str = Form(""),
    confirm: str = Form(""),
    admin: AdminUser = Depends(require_admin_user),
):
    """Replace the live DB with a backup.

    Safety gates (in order):
      1. Admin cookie session (Depends).
      2. Backup token (same token used for downloads).
      3. Explicit `confirm` field must equal the filename — proves the
         operator typed it and isn't replaying a stale form.
      4. Maintenance mode must be ON. We refuse to restore on a live
         system; if you're truly desperate, turn maintenance on first.

    On success we record the pre-restore snapshot filename in the audit log
    so rollback is just another restore call with that filename.
    """
    _check_source(source)
    if not backup_service.is_token_configured():
        raise HTTPException(status_code=503, detail="backup token not configured")
    if not backup_service.verify_token(token):
        audit.record(
            admin=admin,
            action="restore_backup_denied",
            target=f"{source}/{filename}",
            detail="bad_token",
            request=request,
        )
        raise HTTPException(status_code=403, detail="invalid backup token")
    if confirm.strip() != filename:
        raise HTTPException(status_code=400, detail="confirm must equal filename")
    if not maintenance_service.get_status().enabled:
        raise HTTPException(status_code=409, detail="enable maintenance mode before restoring")
    try:
        result = backup_service.restore_from(source, filename)  # type: ignore[arg-type]
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="backup not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record(
        admin=admin,
        action="restore_backup",
        target=f"{source}/{filename}",
        detail={
            "pre_restore_snapshot": result.pre_restore_snapshot,
            "bytes_restored": result.bytes_restored,
        },
        request=request,
    )
    return asdict(result)
