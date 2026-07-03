"""
engine/migrations/migration_006_pa_legacy_netting.py — PA legacy netting migration (ADR-005)

PURPOSE
-------
Implements ADR-005 Stage A migration:

1. Mark all existing bot_orders rows with order_type='virtual_netting' and
   status='filled' as status='legacy_netting'.  These rows are now inert —
   excluded from every ENTRY and EXIT bucket — but are retained permanently
   as an audit trail (Q5 answer: no hard deletion).

2. Add a deprecated_at INTEGER column to cross_reduction_claims.  The column
   records the Unix timestamp of the first reconciler cycle that ran with
   PROPORTIONAL_ALLOCATION=True.  The reconciler writes this value; this
   migration only adds the column (NULL until PA is activated).

Both operations are idempotent — safe to run on every startup.
"""

import sqlite3
import logging

logger = logging.getLogger("Migration006")


def run(db_path: str) -> None:
    """
    Apply migration_006 to the given SQLite database file.
    Idempotent.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        cursor = conn.cursor()

        # Check if migration has already run by checking if deprecated_at column exists
        existing_cols = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(cross_reduction_claims)"
            ).fetchall()
        }

        if "deprecated_at" not in existing_cols:
            # ── 1. Mark virtual_netting rows as legacy_netting ───────────────────
            cursor.execute("""
                UPDATE bot_orders
                SET status = 'legacy_netting',
                    notes  = COALESCE(notes, '') ||
                             ' | PA_MIGRATION_006: inactivated by proportional allocation (ADR-005)'
                WHERE order_type = 'virtual_netting'
                  AND status     = 'filled'
            """)
            migrated_count = cursor.rowcount
            if migrated_count > 0:
                logger.info(
                    f"[MIGRATION-006] Marked {migrated_count} virtual_netting row(s) "
                    f"as legacy_netting."
                )
            else:
                logger.info(
                    "[MIGRATION-006] No virtual_netting rows to migrate "
                    "(already migrated or none exist)."
                )

            # ── 2. Add deprecated_at column to cross_reduction_claims ────────────
            cursor.execute(
                "ALTER TABLE cross_reduction_claims "
                "ADD COLUMN deprecated_at INTEGER"
            )
            logger.info(
                "[MIGRATION-006] Added deprecated_at column to "
                "cross_reduction_claims."
            )
        else:
            logger.info(
                "[MIGRATION-006] deprecated_at column already present in "
                "cross_reduction_claims — skipping migration updates to protect new VNET fills."
            )

        conn.commit()
        logger.info(
            "[MIGRATION-006] ✅ PA legacy netting migration complete "
            "(ADR-005 Stage A)."
        )

    except Exception as e:
        logger.error(f"[MIGRATION-006] Failed: {e}")
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
