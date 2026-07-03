"""
engine/migrations/migration_008_archive_legacy_netting.py — archive legacy virtual netting rows (ADR-006)
"""

import logging
from engine.migrations.migration_base import SafeMigration

logger = logging.getLogger("Migration008")


class Migration008(SafeMigration):
    version = 'migration_008_archive_legacy_netting'
    description = 'Archive legacy virtual_netting rows'
    requires_flat_positions = True  # explicit

    @classmethod
    def run(cls, conn_or_path):
        import sqlite3
        if isinstance(conn_or_path, str):
            conn = sqlite3.connect(conn_or_path, check_same_thread=False)
            close_conn = True
        else:
            conn = conn_or_path
            close_conn = False

        try:
            # Ensure schema_migrations table exists (for standalone/testing)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     TEXT PRIMARY KEY,
                    applied_at  INTEGER NOT NULL,
                    description TEXT
                )
            """)
            conn.commit()

            version = getattr(cls, 'version', cls.__name__)
            
            # Check if already applied
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?",
                (version,)
            ).fetchone()
            
            if applied:
                return  # Already applied — skip entirely

            # FIX 1: Add work-detection short-circuit
            try:
                pending_count = conn.execute("""
                    SELECT COUNT(*) FROM bot_orders 
                    WHERE order_type='virtual_netting' 
                    AND status NOT IN ('archived_legacy','reset_cleared',
                                      'cancelled','canceled','auto_closed')
                """).fetchone()[0]
            except sqlite3.OperationalError:
                # If bot_orders table does not exist yet
                pending_count = 1

            if pending_count == 0:
                # Nothing to archive — migration already applied or no legacy rows
                import time
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations "
                    "(version, applied_at, description) VALUES (?, ?, ?)",
                    (version, int(time.time()), getattr(cls, 'description', ''))
                )
                conn.commit()
                return
            
            # Work exists — enforce INV-33 before proceeding
            cls.preflight_check(conn)
            cls._run_impl(conn)
            
            import time
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(version, applied_at, description) VALUES (?, ?, ?)",
                (version, int(time.time()), getattr(cls, 'description', ''))
            )
            conn.commit()
        finally:
            if close_conn:
                conn.close()

    @classmethod
    def _run_impl(cls, conn):
        cursor = conn.cursor()

        # 1. Update status of existing virtual_netting rows to 'archived_legacy'
        res = cursor.execute("""
            UPDATE bot_orders 
            SET status = 'archived_legacy'
            WHERE order_type = 'virtual_netting' 
              AND status NOT IN ('archived_legacy')
        """)
        archived_count = res.rowcount

        if archived_count > 0:
            logger.info(
                f"[MIGRATION-008] Archived {archived_count} virtual_netting row(s) in bot_orders."
            )
        else:
            logger.info(
                "[MIGRATION-008] No un-archived virtual_netting rows found."
            )

        # Commit the transaction so that the external seal_trade_state call
        # does not run into database table lock issues.
        conn.commit()

        # 2. Force reseal all active bots (is_active = 1)
        from engine.ledger import seal_trade_state

        active_bots = cursor.execute(
            "SELECT id, name FROM bots WHERE is_active = 1"
        ).fetchall()

        logger.info(
            f"[MIGRATION-008] Triggering force-recompute seal_trade_state for {len(active_bots)} active bot(s)..."
        )
        for bot_id, bot_name in active_bots:
            try:
                seal_trade_state(bot_id, force_recompute=True)
                logger.info(
                    f"[MIGRATION-008] ✅ Force-resealed active bot {bot_id} ({bot_name})"
                )
            except Exception as e:
                logger.error(
                    f"[MIGRATION-008] ❌ Failed to reseal bot {bot_id} ({bot_name}): {e}"
                )

        logger.info("[MIGRATION-008] ✅ Migration 008 complete.")


def run(db_path: str) -> None:
    """
    Apply migration_008 to the given SQLite database file.
    Idempotent.
    """
    Migration008.run(db_path)
