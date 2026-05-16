"""CLI entry point for manual operations.

Usage:
    uv run python -m app.cli init-db
    uv run python -m app.cli ingest [--force] [--ats apple google meta ...]
    uv run python -m app.cli stats
    uv run python -m app.cli backfill-country [--all]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.config import settings
from app.database import get_db, import_identity_db, init_db
from app.logging_config import configure_logging


def _setup_logging() -> None:
    configure_logging(settings.log_level)


def cmd_init_db(_: argparse.Namespace) -> int:
    init_db()
    print(f"initialized schema at {settings.db_path}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from app.services.ingestion import run_ingestion

    init_db()
    ats_filter = args.ats if args.ats else None
    result = asyncio.run(run_ingestion(ats_filter=ats_filter, force=args.force))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in ("success", "skipped") else 1


def cmd_stats(_: argparse.Namespace) -> int:
    init_db()
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        dated = conn.execute("SELECT COUNT(*) FROM jobs WHERE posted_at IS NOT NULL").fetchone()[0]
        undated = total - dated
        us = conn.execute("SELECT COUNT(*) FROM jobs WHERE country='US'").fetchone()[0]
        # Effective-date queries (COALESCE) — these drive the dashboard's time windows
        last_24h = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE country='US' AND COALESCE(posted_at, first_seen_at) >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        last_7d = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE country='US' AND COALESCE(posted_at, first_seen_at) >= datetime('now', '-7 days')"
        ).fetchone()[0]
        last_30d = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE country='US' AND COALESCE(posted_at, first_seen_at) >= datetime('now', '-30 days')"
        ).fetchone()[0]
        by_ats = conn.execute(
            "SELECT ats_type, COUNT(*) AS n FROM jobs "
            "GROUP BY ats_type ORDER BY n DESC LIMIT 20"
        ).fetchall()
        saved = conn.execute("SELECT COUNT(*) FROM saved_jobs").fetchone()[0]
        log_rows = conn.execute(
            "SELECT fetched_at, status, rows_ingested, rows_pruned, duration_seconds "
            "FROM manifest_log ORDER BY id DESC LIMIT 5"
        ).fetchall()

    print(f"total jobs:        {total:>10,}")
    print(f"  dated upstream:  {dated:>10,}")
    print(f"  undated (proxy): {undated:>10,}")
    print(f"USA total:         {us:>10,}")
    print(f"USA last 24h:      {last_24h:>10,}     (by COALESCE(posted_at, first_seen_at))")
    print(f"USA last 7d:       {last_7d:>10,}")
    print(f"USA last 30d:      {last_30d:>10,}")
    print(f"saved jobs:        {saved:>10,}")
    print()
    print("top ATS by row count:")
    for row in by_ats:
        print(f"  {row['ats_type'] or '(none)':<25} {row['n']:>10,}")
    print()
    print("recent ingest log:")
    for row in log_rows:
        print(
            f"  {row['fetched_at']}  {row['status']:<8}  "
            f"ingested={row['rows_ingested'] or 0:>8}  "
            f"pruned={row['rows_pruned'] or 0:>8}  "
            f"{(row['duration_seconds'] or 0):.1f}s"
        )
    return 0


def cmd_backfill_country(args: argparse.Namespace) -> int:
    """Re-run extract_country over existing rows.

    Default mode rewrites only rows where country is NULL or '' — useful after
    adding new detection patterns (IN, EU, …) to retag previously-untagged
    rows without touching the existing US set. --all rewrites every row.
    """
    from app.services.ingestion import extract_country

    init_db()
    where = "" if args.all else "WHERE country IS NULL OR country = ''"
    with get_db() as conn:
        rows = conn.execute(f"SELECT id, location, country FROM jobs {where}").fetchall()
        log = logging.getLogger("backfill")
        log.info("scanning %d rows", len(rows))
        updates: list[tuple[str | None, int]] = []
        for r in rows:
            new = extract_country(r["location"])
            if new != r["country"]:
                updates.append((new, r["id"]))
        log.info("updating %d rows", len(updates))
        conn.execute("BEGIN")
        try:
            conn.executemany("UPDATE jobs SET country=? WHERE id=?", updates)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        counts = conn.execute(
            "SELECT COALESCE(NULLIF(country, ''), '(none)') AS c, COUNT(*) AS n "
            "FROM jobs GROUP BY c ORDER BY n DESC"
        ).fetchall()
    print(f"scanned:  {len(rows):>10,}")
    print(f"updated:  {len(updates):>10,}")
    print()
    print("country distribution:")
    for row in counts:
        print(f"  {row['c']:<10} {row['n']:>10,}")
    return 0


def cmd_sync_company_catalogs(args: argparse.Namespace) -> int:
    from app.services.company_catalog_sync import sync_company_catalogs

    result = asyncio.run(
        sync_company_catalogs(
            repo=args.repo,
            ref=args.ref,
            prune=args.prune,
            dry_run=args.dry_run,
        )
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_import_identity_db(args: argparse.Namespace) -> int:
    init_db()
    result = import_identity_db()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_verify_now(args: argparse.Namespace) -> int:
    """Manually drive one verifier batch — bypasses APScheduler entirely.

    Useful when you've been running with --reload (which keeps resetting
    the in-process scheduler) and you want immediate proof the verifier
    works against your real corpus.
    """
    import asyncio

    from app.services import verifier as verifier_svc

    init_db()
    mode = args.mode
    if mode == "sweep":
        result = asyncio.run(verifier_svc.drain_periodic_sweep(
            batch_size=args.batch if args.batch and args.batch > 0 else None
        ))
    elif mode == "suspected":
        result = asyncio.run(verifier_svc.drain_suspected(batch_size=args.batch or None))
    elif mode == "drops":
        result = asyncio.run(verifier_svc.drain_manifest_drops(
            batch_size=args.batch if args.batch and args.batch > 0 else 200
        ))
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


def main() -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="create schema if missing")
    p_init.set_defaults(func=cmd_init_db)

    p_ing = sub.add_parser("ingest", help="run an ingestion cycle")
    p_ing.add_argument("--force", action="store_true",
                       help="ignore manifest_log freshness check")
    p_ing.add_argument("--ats", nargs="+", default=None,
                       help="restrict to these ATS names (default: all)")
    p_ing.set_defaults(func=cmd_ingest)

    p_stats = sub.add_parser("stats", help="show DB stats")
    p_stats.set_defaults(func=cmd_stats)

    p_bf = sub.add_parser(
        "backfill-country",
        help="re-run country detection on existing rows (default: only NULL/empty)",
    )
    p_bf.add_argument("--all", action="store_true",
                      help="re-tag every row, not just NULL/empty ones")
    p_bf.set_defaults(func=cmd_backfill_country)

    p_sync = sub.add_parser(
        "sync-company-catalogs",
        help="sync vendored ats-companies/*.csv from upstream GitHub repo",
    )
    p_sync.add_argument(
        "--repo",
        default="kalil0321/ats-scrapers",
        help="GitHub repo in owner/name format (default: kalil0321/ats-scrapers)",
    )
    p_sync.add_argument(
        "--ref",
        default="main",
        help="branch, tag, or commit to pull from (default: main)",
    )
    p_sync.add_argument(
        "--no-prune",
        dest="prune",
        action="store_false",
        help="do not delete local CSV files that no longer exist upstream",
    )
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="show summary without writing files",
    )
    p_sync.set_defaults(func=cmd_sync_company_catalogs, prune=True)

    p_import = sub.add_parser(
        "import-identity-db",
        help="one-time import of identity-service data into dashboard applyd.db",
    )
    p_import.set_defaults(func=cmd_import_identity_db)

    p_verify = sub.add_parser(
        "verify-now",
        help="run one verifier batch right now (bypasses the scheduler)",
    )
    p_verify.add_argument(
        "--mode",
        choices=("sweep", "suspected", "drops"),
        default="sweep",
        help="sweep = active corpus; suspected = drain suspected pool; drops = manifest-drop sweep",
    )
    p_verify.add_argument(
        "--batch",
        type=int,
        default=0,
        help="batch size (0 = auto from settings)",
    )
    p_verify.set_defaults(func=cmd_verify_now)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
