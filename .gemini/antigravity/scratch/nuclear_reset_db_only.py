"""
nuclear_reset_db_only.py — Database Wipe

Strategy:
  1. Run a full DB wipe: all bots → Scanning, trades zeroed, active_positions cleared
  2. Let the engine restart clean — no ghost state anywhere
"""
import sys
import time
import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("NUCLEAR_RESET_DB")

DB_PATH = "crypto_bot.db"

logger.info("======================================================================")
logger.info("NUCLEAR DB WIPE — Zeroing all trades, setting bots to Scanning")
logger.info("======================================================================")

conn = sqlite3.connect(DB_PATH, timeout=30)
conn.row_factory = sqlite3.Row
try:
    conn.execute("BEGIN IMMEDIATE")
    ts_now = int(time.time())
    
    # 1. Zero ALL trades rows
    conn.execute("""
        UPDATE trades SET
            current_step    = 0,
            total_invested  = 0,
            avg_entry_price = 0,
            target_tp_price = 0,
            open_qty        = 0,
            entry_confirmed = 0,
            entry_order_id  = NULL,
            tp_order_id     = NULL,
            bot_position_id = NULL,
            wipe_wall_ts    = ?,
            cycle_phase     = 'IDLE',
            cycle_start_time = ?,
            basket_start_time = ?
    """, (ts_now, ts_now, ts_now))
    logger.info("  ✅ Zeroed all trades accumulators")
    
    # 2. Set all active bots to Scanning
    conn.execute("""
        UPDATE bots SET status = 'Scanning'
        WHERE is_active = 1 AND status != 'STOPPED'
    """)
    logger.info("  ✅ Set all active bots to Scanning")
    
    # 3. Mark all open/new orders as auto_closed (exchange reality is flat)
    conn.execute("""
        UPDATE bot_orders
        SET status = 'auto_closed', updated_at = ?
        WHERE status IN ('open', 'new', 'placing')
    """, (ts_now,))
    logger.info("  ✅ Marked all pending orders as auto_closed")
    
    # 4. Clear active_positions (exchange is flat now)
    conn.execute("DELETE FROM active_positions")
    logger.info("  ✅ Cleared active_positions table")
    
    # 5. Clear any bot error flags so they don't block startup
    conn.execute("""
        UPDATE bots SET last_error = NULL, last_error_time = NULL
        WHERE is_active = 1
    """)
    logger.info("  ✅ Cleared bot error flags")
    
    conn.commit()
    logger.info("  ✅ DB nuclear wipe committed")
    
    # Report final state
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bots WHERE is_active=1")
    total_bots = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM bots WHERE is_active=1 AND status='Scanning'")
    scanning = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM active_positions")
    active_pos = cur.fetchone()[0]
    logger.info(f"\n  Final DB state: {total_bots} active bots, {scanning} Scanning, {active_pos} active_positions rows")
    
except Exception as e:
    conn.rollback()
    logger.error(f"  ❌ DB wipe failed: {e}")
    raise
finally:
    conn.close()

logger.info("\n======================================================================")
logger.info("NUCLEAR DB WIPE COMPLETE")
logger.info("======================================================================")
