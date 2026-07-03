"""
engine/migrations/migration_010_unowned_position_alerts.py — create unowned_position_alerts table

Schema-only migration: requires_flat_positions = False.
Creates the table for storing unowned exchange position alerts.
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration010")


class Migration010(SafeMigration):
    version = 'migration_010_unowned_position_alerts'
    description = 'create unowned_position_alerts table'
    requires_flat_positions = False  # schema-only, safe to run with open positions

    @classmethod
    def _run_impl(cls, conn):
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS unowned_position_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                pair TEXT NOT NULL,
                normalized_pair TEXT NOT NULL,
                exchange_qty REAL NOT NULL,
                db_qty REAL NOT NULL,
                detected_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_review',
                notes TEXT
            );
        """)
        logger.info("[MIGRATION-010] Created unowned_position_alerts table.")
        logger.info("[MIGRATION-010] ✅ Migration 010 complete.")


def run(db_path: str) -> None:
    Migration010.run(db_path)
