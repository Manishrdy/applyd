"""Sync vendored ATS company CSV catalogs from upstream GitHub."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DASHBOARD_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_DIR = _DASHBOARD_ROOT / "vendor" / "ats-scrapers" / "ats-companies"
_GITHUB_API = "https://api.github.com"


class CatalogSyncError(RuntimeError):
    """Raised when upstream catalog sync cannot proceed."""


async def sync_company_catalogs(
    *,
    repo: str = "kalil0321/ats-scrapers",
    ref: str = "main",
    prune: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sync upstream ats-companies/*.csv and README.md into vendored tree."""
    if "/" not in repo:
        raise CatalogSyncError("repo must look like <owner>/<name>")
    if not _CATALOG_DIR.exists():
        raise CatalogSyncError(f"catalog dir not found: {_CATALOG_DIR}")

    owner, name = repo.split("/", 1)
    list_url = f"{_GITHUB_API}/repos/{owner}/{name}/contents/ats-companies"
    params = {"ref": ref}

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(list_url, params=params, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        entries = r.json()

        keep: dict[str, bytes] = {}
        for e in entries:
            if e.get("type") != "file":
                continue
            file_name = str(e.get("name") or "")
            if not (file_name.endswith(".csv") or file_name == "README.md"):
                continue
            download_url = e.get("download_url")
            if not download_url:
                continue
            fr = await client.get(download_url)
            fr.raise_for_status()
            keep[file_name] = fr.content

    if not keep:
        raise CatalogSyncError("no ats company catalog files found upstream")

    added = 0
    updated = 0
    unchanged = 0
    removed = 0

    for file_name, content in sorted(keep.items()):
        dst = _CATALOG_DIR / file_name
        if not dst.exists():
            added += 1
            if not dry_run:
                dst.write_bytes(content)
            continue
        old = dst.read_bytes()
        if old == content:
            unchanged += 1
            continue
        updated += 1
        if not dry_run:
            dst.write_bytes(content)

    if prune:
        expected = set(keep.keys())
        for dst in _CATALOG_DIR.iterdir():
            if not dst.is_file():
                continue
            if not (dst.name.endswith(".csv") or dst.name == "README.md"):
                continue
            if dst.name in expected:
                continue
            removed += 1
            if not dry_run:
                dst.unlink()

    summary = {
        "status": "success",
        "repo": repo,
        "ref": ref,
        "catalog_dir": str(_CATALOG_DIR),
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
        "dry_run": dry_run,
        "pruned": prune,
        "total_upstream_files": len(keep),
    }
    log.info("catalog sync summary: %s", summary)
    return summary

