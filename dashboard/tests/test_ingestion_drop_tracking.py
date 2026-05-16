"""Ingestion: missed-cycle counter + expired prune behaviour."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database import get_db
from app.services.ingestion import (
    mark_manifest_drops,
    prune_old,
    prune_verification_log,
)


def test_mark_manifest_drops_increments_old_rows(test_db_path):
    """Rows with last_seen_in_manifest_at older than the cycle start get
    missed_ingest_cycles bumped; younger rows do not."""
    with get_db(test_db_path) as conn:
        # All seeded rows were inserted in this run, so set ages explicitly.
        conn.execute(
            "UPDATE jobs SET last_seen_in_manifest_at = '2026-05-01 00:00:00', "
            "missed_ingest_cycles = 0 WHERE id IN (1, 2)"
        )
        conn.execute(
            "UPDATE jobs SET last_seen_in_manifest_at = ?, missed_ingest_cycles = 0 "
            "WHERE id = 3",
            (datetime.now(timezone.utc).isoformat(),),
        )

        cycle_start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = mark_manifest_drops(conn, cycle_start)
        assert rows >= 2

        bumped = conn.execute(
            "SELECT id, missed_ingest_cycles FROM jobs WHERE id IN (1, 2, 3) "
            "ORDER BY id"
        ).fetchall()
        # Rows 1 and 2 were old → bumped. Row 3 was current → unchanged.
        bumped_by_id = {r["id"]: r["missed_ingest_cycles"] for r in bumped}
        assert bumped_by_id[1] == 1
        assert bumped_by_id[2] == 1
        assert bumped_by_id[3] == 0


def test_prune_old_keeps_recent_expired(lifecycle_seed):
    """An expired row marked <30d ago should NOT be pruned."""
    with get_db(lifecycle_seed) as conn:
        # Bump the expired row's verification_status_at to "today" so the
        # prune window keeps it (the seed value is a fixed ISO date).
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET verification_status_at = ? WHERE id = 8",
            (now_iso,),
        )
        # Bump first_seen_at so the non-expired branch (rolling window)
        # doesn't sweep it for being undated/old.
        conn.execute(
            "UPDATE jobs SET first_seen_at = ?, posted_at = ? WHERE id = 8",
            (now_iso, now_iso),
        )
        prune_old(conn, days=30, current_cycle="2026-05-15")
        row = conn.execute(
            "SELECT id, verification_status FROM jobs WHERE id = 8"
        ).fetchone()
        assert row is not None
        assert row["verification_status"] == "expired"


def test_prune_old_deletes_long_expired(lifecycle_seed):
    """An expired row with status_at >30d ago should be deleted."""
    with get_db(lifecycle_seed) as conn:
        # Force row 8 to look like it expired 45 days ago.
        long_ago = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        conn.execute(
            "UPDATE jobs SET verification_status_at = ? WHERE id = 8",
            (long_ago,),
        )
        prune_old(conn, days=30, current_cycle="2026-05-15")
        row = conn.execute("SELECT id FROM jobs WHERE id = 8").fetchone()
        assert row is None


def test_prune_verification_log_ttl(test_db_path):
    """Log rows older than 90 days are dropped."""
    with get_db(test_db_path) as conn:
        old_iso = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        new_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO job_verification_log (job_id, checked_at, trigger, result) "
            "VALUES (1, ?, 'periodic', 'active'), (1, ?, 'periodic', 'active')",
            (old_iso, new_iso),
        )
        deleted = prune_verification_log(conn)
        assert deleted >= 1
        remaining = conn.execute(
            "SELECT COUNT(*) FROM job_verification_log WHERE job_id = 1"
        ).fetchone()[0]
        assert remaining == 1
