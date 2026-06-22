"""
engine/migrations/migration_004_cross_reduction_claims.py — cross_reduction_claims idempotency table (INV-21)

PURPOSE
-------
Implements ARCHITECTURE INVARIANT INV-21: apply_oneway_entry_cross_reduction() is a
singleton per (source_order_id, target_bot_id). This table provides an atomic
INSERT OR IGNORE guard at the DB layer, preventing duplicate netting rows from being
written when concurrent paths trigger cross-reduction.

TABLE DESIGN
------------
- UNIQUE constraint: (source_order_id, target_bot_id)
- source_order_id: the entry order_id that triggered the netting.
- source_bot_id: the bot that just filled (the SHORT/LONG entrant).
- target_bot_id: the opposite-side sibling bot whose open_qty is being reduced.
- reduction_qty: the amount reduced.
- claimed_at: unix timestamp of the claim.
"""

import sqlite3
import logging

logger = logging.getLogger("Migration004")


def run(db_path: str) -> None:
    """
    Apply migration_004 to the given SQLite database file.
    Idempotent.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()

        # ── Create cross_reduction_claims table ───────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cross_reduction_claims (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                source_order_id   TEXT    NOT NULL,
                source_bot_id     INTEGER NOT NULL,
                target_bot_id     INTEGER NOT NULL,
                reduction_qty     REAL    NOT NULL,
                claimed_at        INTEGER NOT NULL,
                UNIQUE (source_order_id, target_bot_id)
            )
        """)

        # ── Unique index ──────────────────────────────────────────────────────
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cross_reduction_claims_key
            ON cross_reduction_claims (source_order_id, target_bot_id)
        """)

        conn.commit()
        logger.info(
            "[MIGRATION-004] ✅ cross_reduction_claims table ready "
            "(INV-21: cross-reduction idempotency guard)."
        )

    except Exception as e:
        logger.error(f"[MIGRATION-004] Failed: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
