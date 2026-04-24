"""
engine/migrations/migration_001_v2_schema.py — v2.0 + v2.1 Database Schema Migration

Safe to run multiple times (idempotent). All operations check before modifying.

Changes:
  1. Add `cumulative_filled REAL DEFAULT 0` to bot_orders
  2. Ensure `position_side TEXT` exists on bot_orders
  3. Ensure `cycle_id INTEGER` exists on bot_orders
  4. Add `notes TEXT` column to bot_orders if missing
  5. Ensure reconciliation_logs table exists
  6. [v2.1] Add `open_qty REAL DEFAULT 0` to trades
     — The authoritative running position accumulator. Incremented on every
       confirmed entry fill, decremented on every TP/close fill. TP is placed
       for exactly this value. Eliminates float-sum dust permanently.
  7. [v2.1] Add `wipe_wall_ts INTEGER DEFAULT 0` to trades
     — Written on every reset (TP, Force SL, Market Close). The offline fill
       scanner skips any exchange fill older than this timestamp, permanently
       preventing restart-contamination of the DB from historical fills.
"""

import logging
import sqlite3

logger = logging.getLogger("Migration001")


def run(db_path: str = None) -> dict:
    """
    Execute all schema migrations. Safe to call multiple times.

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

        # ---------- Migration 6 [v2.1]: open_qty on trades ----------
        # The authoritative running position size accumulator.
        # Eliminates float-sum dust: TP is placed for exactly this value,
        # incremented/decremented atomically with every exchange-confirmed fill.
        if _table_exists("trades") and not _column_exists("trades", "open_qty"):
            conn.execute(
                "ALTER TABLE trades ADD COLUMN open_qty REAL DEFAULT 0"
            )
            conn.commit()
            logger.info("✅ [M001] Added open_qty to trades (v2.1 accumulator).")
            applied.append("trades.open_qty")
        else:
            skipped.append("trades.open_qty (already exists or table missing)")

        # ---------- Migration 7 [v2.1]: wipe_wall_ts on trades ----------
        # Written on every reset. Gates offline fill scanner to skip pre-wipe fills.
        # Permanently ends the restart-contamination loop.
        if _table_exists("trades") and not _column_exists("trades", "wipe_wall_ts"):
            conn.execute(
                "ALTER TABLE trades ADD COLUMN wipe_wall_ts INTEGER DEFAULT 0"
            )
            conn.commit()
            logger.info("✅ [M001] Added wipe_wall_ts to trades (v2.1 session boundary).")
            applied.append("trades.wipe_wall_ts")
        else:
            skipped.append("trades.wipe_wall_ts (already exists or table missing)")

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
