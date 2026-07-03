"""
engine/migrations/migration_007_cascade_started_at.py — add cascade_started_at column
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration007")


class Migration007(SafeMigration):
    version = 'migration_007_cascade_started_at'
    description = 'add cascade_started_at column'
    requires_flat_positions = False

    @classmethod
    def _run_impl(cls, conn):
        cursor = conn.cursor()

        # Check if migration has already run by checking if cascade_started_at column exists
        existing_cols = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(bots)"
            ).fetchall()
        }

        if "cascade_started_at" not in existing_cols:
            cursor.execute(
                "ALTER TABLE bots "
                "ADD COLUMN cascade_started_at INTEGER DEFAULT 0"
            )
            logger.info(
                "[MIGRATION-007] Added cascade_started_at column to bots table."
            )
        else:
            logger.info(
                "[MIGRATION-007] cascade_started_at column already present in bots table — skipping."
            )

        logger.info("[MIGRATION-007] ✅ Migration 007 complete.")


def run(db_path: str) -> None:
    Migration007.run(db_path)
