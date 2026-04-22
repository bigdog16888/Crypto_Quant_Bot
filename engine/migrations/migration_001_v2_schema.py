"""
engine/migrations/001_v2_schema.py — v2.0 Database Schema Migration

One-time migrations to initialize v2.0 schema changes. Safe to run multiple
times (idempotent) — all operations check before modifying.

Run order (called from engine startup or manually):
    python -m engine.migrations.001_v2_schema

Changes:
  1. Add `cumulative_filled REAL DEFAULT 0` to bot_orders
     (stores the Binance WS 'z' field — authoritative cumulative fill qty)
  2. Ensure `position_side TEXT` exists on bot_orders
     (needed for LONG/SHORT segregation in recompute_invested_from_orders)
  3. Ensure `cycle_id INTEGER` exists on bot_orders
     (needed for cycle-aware ledger queries)
  4. Add `notes TEXT` column to bot_orders if missing
     (for adoption/heal audit trail)
"""

import logging
import sqlite3

logger = logging.getLogger("Migration001")


def run(db_path: str = None) -> dict:
    """
    Execute all v2.0 schema migrations. Safe to call multiple times.

    Args:
        db_path: Explicit path to SQLite DB. If None, uses engine.database.DB_PATH.

    Returns:
        dict with keys: applied (list of applied migrations), skipped (list of skipped)
    """
    if db_path is None:
        from engine.database import DB_PATH
        db_path = DB_PATH

    applied = []
    skipped = []

    try:
        conn = sqlite3.connect(db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        # ---------- Helper: check if column exists ----------
        def _column_exists(table: str, column: str) -> bool:
            cur.execute(f"PRAGMA table_info({table})")
            return any(row[1] == column for row in cur.fetchall())

        def _table_exists(table: str) -> bool:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            return cur.fetchone() is not None

        # ---------- Migration 1: cumulative_filled on bot_orders ----------
        if _table_exists("bot_orders") and not _column_exists("bot_orders", "cumulative_filled"):
            conn.execute(
                "ALTER TABLE bot_orders ADD COLUMN cumulative_filled REAL DEFAULT 0"
            )
            conn.commit()
            logger.info("✅ [M001] Added cumulative_filled to bot_orders.")
            applied.append("bot_orders.cumulative_filled")
        else:
            skipped.append("bot_orders.cumulative_filled (already exists or table missing)")

        # ---------- Migration 2: position_side on bot_orders ----------
        if _table_exists("bot_orders") and not _column_exists("bot_orders", "position_side"):
            conn.execute(
                "ALTER TABLE bot_orders ADD COLUMN position_side TEXT"
            )
            conn.commit()
            logger.info("✅ [M001] Added position_side to bot_orders.")
            applied.append("bot_orders.position_side")
        else:
            skipped.append("bot_orders.position_side (already exists or table missing)")

        # ---------- Migration 3: cycle_id on bot_orders ----------
        if _table_exists("bot_orders") and not _column_exists("bot_orders", "cycle_id"):
            conn.execute(
                "ALTER TABLE bot_orders ADD COLUMN cycle_id INTEGER"
            )
            conn.commit()
            logger.info("✅ [M001] Added cycle_id to bot_orders.")
            applied.append("bot_orders.cycle_id")
        else:
            skipped.append("bot_orders.cycle_id (already exists or table missing)")

        # ---------- Migration 4: notes on bot_orders ----------
        if _table_exists("bot_orders") and not _column_exists("bot_orders", "notes"):
            conn.execute(
                "ALTER TABLE bot_orders ADD COLUMN notes TEXT"
            )
            conn.commit()
            logger.info("✅ [M001] Added notes to bot_orders.")
            applied.append("bot_orders.notes")
        else:
            skipped.append("bot_orders.notes (already exists or table missing)")

        # ---------- Migration 5: ensure reconciliation_logs table exists ----------
        if not _table_exists("reconciliation_logs"):
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reconciliation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id INTEGER,
                    pair TEXT,
                    action TEXT,
                    details TEXT,
                    timestamp INTEGER
                )
            """)
            conn.commit()
            logger.info("✅ [M001] Created reconciliation_logs table.")
            applied.append("reconciliation_logs (new table)")
        else:
            skipped.append("reconciliation_logs (already exists)")

        conn.close()
        logger.info(
            f"[M001] Migration complete. Applied: {len(applied)}, Skipped: {len(skipped)}."
        )
        return {"applied": applied, "skipped": skipped}

    except Exception as e:
        logger.error(f"[M001] Migration failed: {e}")
        return {"applied": applied, "skipped": skipped, "error": str(e)}


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    print(f"\nResult: {result}")
