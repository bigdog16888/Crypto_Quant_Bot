"""
engine/migrations/migration_005_cid_too_long.py — clean up existing oversized CIDs
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration005")


class Migration005(SafeMigration):
    version = 'migration_005_cid_too_long'
    description = 'clean up existing oversized CIDs'
    requires_flat_positions = False

    @classmethod
    def _run_impl(cls, conn):
        cursor = conn.cursor()

        # Update matching rows
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'failed', 
                notes = COALESCE(notes, '') || ' | CID_TOO_LONG_MIGRATION: orphaned, exceeds Binance 36-char limit'
            WHERE status = 'pending_placement'
            AND (
                client_order_id LIKE 'CQB_%_DRIFT_ENFORCE_RESET_%'
                OR client_order_id LIKE 'CQB_%_DRIFT_GHOST_WIPE_%'
                OR client_order_id LIKE 'CQB_%_DRIFT_%'
            )
            AND LENGTH(client_order_id) > 36
        """)

        updated_count = cursor.rowcount

        if updated_count > 0:
            logger.info(
                f"[MIGRATION-005] ✅ Cleaned up {updated_count} orphaned oversized client_order_id rows."
            )
        else:
            logger.debug("[MIGRATION-005] No oversized client_order_id rows found to clean.")


def run(db_path: str) -> None:
    Migration005.run(db_path)
