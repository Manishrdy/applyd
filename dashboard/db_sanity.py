#!/usr/bin/env python3
"""SQLite jobs sanity/prune helper.

Usage:
  python db_sanity.py
  python db_sanity.py --prune
  python db_sanity.py --prune --vacuum
  python db_sanity.py --days 30 --db ./data/applyd.db
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Stats:
    total_jobs: int
    in_window_jobs: int
    out_of_window_jobs: int
    null_both_dates: int


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _int_query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0] or 0)


def compute_stats(conn: sqlite3.Connection, cutoff_iso: str) -> Stats:
    total = _int_query(conn, "SELECT COUNT(*) FROM jobs")
    in_window = _int_query(
        conn,
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(posted_at, first_seen_at) >= ?",
        (cutoff_iso,),
    )
    out_window = _int_query(
        conn,
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(posted_at, first_seen_at) < ?",
        (cutoff_iso,),
    )
    null_both = _int_query(
        conn,
        "SELECT COUNT(*) FROM jobs "
        "WHERE posted_at IS NULL AND first_seen_at IS NULL",
    )
    return Stats(total, in_window, out_window, null_both)


def estimate_jobs_storage(conn: sqlite3.Connection) -> tuple[int | None, int | None]:
    """Returns (jobs_related_bytes, total_db_bytes) from dbstat if available.

    jobs_related_bytes includes jobs table + jobs indexes + jobs_fts structures.
    """
    try:
        total = _int_query(conn, "SELECT SUM(pgsize) FROM dbstat")
        jobs_related = _int_query(
            conn,
            "SELECT SUM(pgsize) FROM dbstat "
            "WHERE name = 'jobs' "
            "   OR name LIKE 'idx_jobs_%' "
            "   OR name LIKE 'jobs_fts%'",
        )
        return jobs_related, total
    except sqlite3.Error:
        return None, None


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    i = 0
    while x >= 1024.0 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.2f} {units[i]}"


def prune(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    cur = conn.execute(
        "DELETE FROM jobs WHERE COALESCE(posted_at, first_seen_at) < ?",
        (cutoff_iso,),
    )
    return int(cur.rowcount or 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect/prune jobs by rolling-day window")
    parser.add_argument("--db", default="./data/applyd.db", help="Path to sqlite DB")
    parser.add_argument("--days", type=int, default=30, help="Rolling window days")
    parser.add_argument("--prune", action="store_true", help="Delete rows outside rolling window")
    parser.add_argument("--vacuum", action="store_true", help="Run VACUUM after prune")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, args.days))
    cutoff_iso = cutoff.isoformat()

    file_before = db_path.stat().st_size
    with _connect(db_path) as conn:
        before = compute_stats(conn, cutoff_iso)
        jobs_bytes, total_bytes = estimate_jobs_storage(conn)

        print(f"DB: {db_path}")
        print(f"Cutoff (UTC): {cutoff_iso}")
        print("\nBefore:")
        print(f"  total jobs                  : {before.total_jobs:,}")
        print(f"  jobs in last {args.days}d          : {before.in_window_jobs:,}")
        print(f"  jobs outside last {args.days}d     : {before.out_of_window_jobs:,}")
        print(f"  jobs with both dates NULL   : {before.null_both_dates:,}")
        print(f"  db file size                : {fmt_bytes(file_before)}")
        if jobs_bytes is not None and total_bytes:
            ratio = jobs_bytes / total_bytes if total_bytes else 0
            est_in = int(jobs_bytes * (before.in_window_jobs / before.total_jobs)) if before.total_jobs else 0
            est_out = int(jobs_bytes * (before.out_of_window_jobs / before.total_jobs)) if before.total_jobs else 0
            print(f"  jobs-related pages          : {fmt_bytes(jobs_bytes)} ({ratio:.1%} of DB pages)")
            print(f"  est jobs bytes in-window    : {fmt_bytes(est_in)} (row-count proportional)")
            print(f"  est jobs bytes out-window   : {fmt_bytes(est_out)} (row-count proportional)")
        else:
            print("  jobs size estimates         : n/a (dbstat unavailable)")

        deleted = 0
        if args.prune:
            deleted = prune(conn, cutoff_iso)
            print(f"\nPrune deleted rows            : {deleted:,}")
            if args.vacuum:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                print("Vacuum                        : completed")

        after = compute_stats(conn, cutoff_iso)

    file_after = db_path.stat().st_size
    print("\nAfter:")
    print(f"  total jobs                  : {after.total_jobs:,}")
    print(f"  jobs in last {args.days}d          : {after.in_window_jobs:,}")
    print(f"  jobs outside last {args.days}d     : {after.out_of_window_jobs:,}")
    print(f"  db file size                : {fmt_bytes(file_after)}")
    print(f"  file size delta             : {fmt_bytes(file_after - file_before)}")


if __name__ == "__main__":
    main()
