"""
engine/migrations/migration_003_fill_claims.py — fill_claims idempotency table (INV-20)
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration003")


class Migration003(SafeMigration):
    version = 'migration_003_fill_claims'
    description = 'fill_claims table'
    requires_flat_positions = False

    @classmethod
    def _run_impl(cls, conn):
        cursor = conn.cursor()

        # ── Create fill_claims table ───────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fill_claims (
                bot_id     INTEGER NOT NULL,
                order_id   TEXT    NOT NULL,
                caller     TEXT    NOT NULL DEFAULT '',
                claimed_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bot_id, order_id)
            )
        """)

        # ── Unique index on (bot_id, order_id) ────────────────────────────────
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_fill_claims_key
            ON fill_claims (bot_id, order_id)
        """)

        logger.info(
            "[MIGRATION-003] ✅ fill_claims table ready "
            "(INV-20: credit_fill singleton guard)."
        )


def run(db_path: str) -> None:
    Migration003.run(db_path)
