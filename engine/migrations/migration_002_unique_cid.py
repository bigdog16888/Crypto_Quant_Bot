"""
engine/migrations/migration_002_unique_cid.py — Unique client_order_id Index Migration

Safe to run multiple times (idempotent).
Checks and heals duplicate (bot_id, client_order_id) records in bot_orders before creating the unique index.
"""

import logging
import sqlite3

logger = logging.getLogger("Migration002")


def run(db_path: str = None) -> dict:
    """
    Execute unique index migration. Safe to call multiple times.

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

        # ---------- Helper: check if index exists ----------
        def _index_exists(index_name: str) -> bool:
            cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
            return cur.fetchone() is not None

        def _table_exists(table: str) -> bool:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            return cur.fetchone() is not None

        if not _table_exists("bot_orders"):
            logger.warning("⚠️ [M002] bot_orders table does not exist. Skipping.")
            skipped.append("bot_orders table missing")
            conn.close()
            return {"applied": applied, "skipped": skipped}

        # ---------- 1. Detect and resolve duplicate CIDs ----------
        cur.execute("""
            SELECT bot_id, client_order_id, COUNT(*)
            FROM bot_orders
            WHERE client_order_id IS NOT NULL AND client_order_id != ''
            GROUP BY bot_id, client_order_id
            HAVING COUNT(*) > 1
        """)
        duplicates = cur.fetchall()

        if duplicates:
            logger.info(f"⏳ [M002] Found {len(duplicates)} duplicate client_order_id groups. Resolving...")
            for bot_id, cid, count in duplicates:
                # Get all rows matching this group
                cur.execute("""
                    SELECT id FROM bot_orders 
                    WHERE bot_id = ? AND client_order_id = ? 
                    ORDER BY id
                """, (bot_id, cid))
                rows = cur.fetchall()
                
                # Keep the first one, rename the rest starting from index 1
                for idx, (row_id,) in enumerate(rows[1:], 1):
                    suffix_idx = idx
                    while True:
                        new_cid = f"{cid}_{suffix_idx}"
                        # Check if new_cid already exists for this bot
                        cur.execute("""
                            SELECT COUNT(*) FROM bot_orders 
                            WHERE bot_id = ? AND client_order_id = ?
                        """, (bot_id, new_cid))
                        if cur.fetchone()[0] == 0:
                            break
                        suffix_idx += 1
                    
                    cur.execute("UPDATE bot_orders SET client_order_id = ? WHERE id = ?", (new_cid, row_id))
                    logger.info(f"🩹 [M002] Renamed duplicate client_order_id on bot_orders row {row_id}: {cid} -> {new_cid}")
            
            conn.commit()
            applied.append("deduplicate_existing_cids")
        else:
            skipped.append("deduplicate_existing_cids (no duplicates found)")

        # ---------- 2. Create the Unique Index ----------
        if not _index_exists("idx_bot_orders_bot_cid"):
            conn.execute("""
                CREATE UNIQUE INDEX idx_bot_orders_bot_cid 
                ON bot_orders (bot_id, client_order_id);
            """)
            conn.commit()
            logger.info("✅ [M002] Created unique index idx_bot_orders_bot_cid on bot_orders(bot_id, client_order_id).")
            applied.append("unique_index_created")
        else:
            skipped.append("idx_bot_orders_bot_cid (already exists)")

        conn.close()
        logger.info(
            f"[M002] Migration complete. Applied: {len(applied)}, Skipped: {len(skipped)}."
        )
        return {"applied": applied, "skipped": skipped}

    except Exception as e:
        logger.error(f"[M002] Migration failed: {e}")
        return {"applied": applied, "skipped": skipped, "error": str(e)}


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run()
    print(f"\nResult: {result}")
