"""CLI entry point for manual operations.

Usage:
    uv run python -m app.cli init-db
    uv run python -m app.cli ingest [--force] [--ats apple google meta ...]
    uv run python -m app.cli stats
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.config import settings
from app.database import get_db, init_db


def _setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
