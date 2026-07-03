"""
engine/migrations/migration_004_cross_reduction_claims.py — cross_reduction_claims idempotency table (INV-21)
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration004")


class Migration004(SafeMigration):
    version = 'migration_004_cross_reduction_claims'
    description = 'cross_reduction_claims'
    requires_flat_positions = False

    @classmethod
    def _run_impl(cls, conn):
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

        logger.info(
            "[MIGRATION-004] ✅ cross_reduction_claims table ready "
            "(INV-21: cross-reduction idempotency guard)."
        )


def run(db_path: str) -> None:
    Migration004.run(db_path)
