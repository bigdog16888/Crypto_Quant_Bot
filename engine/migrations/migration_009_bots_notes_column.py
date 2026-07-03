"""
engine/migrations/migration_009_bots_notes_column.py — add notes column to bots table

Schema-only migration: requires_flat_positions = False.
Resolves the graceful-degradation WARNING from engine/oneway_netting.py where
manual-review flags were being written to JSON cache because bots.notes was absent.
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration009")


class Migration009(SafeMigration):
    version = 'migration_009_bots_notes_column'
    description = 'add notes column to bots table'
    requires_flat_positions = False  # schema-only, safe to run with open positions

    @classmethod
    def _run_impl(cls, conn):
        cursor = conn.cursor()

        # Idempotency guard — skip if column already exists
        existing_cols = {
            row[1]
            for row in cursor.execute("PRAGMA table_info(bots)").fetchall()
        }

        if "notes" not in existing_cols:
            cursor.execute("ALTER TABLE bots ADD COLUMN notes TEXT DEFAULT NULL")
            logger.info(
                "[MIGRATION-009] Added notes column to bots table — "
                "manual review flags from oneway_netting will now persist to DB."
            )
        else:
            logger.info(
                "[MIGRATION-009] notes column already present in bots table — skipping."
            )

        logger.info("[MIGRATION-009] ✅ Migration 009 complete.")


def run(db_path: str) -> None:
    Migration009.run(db_path)
