import sys
import os
import sqlite3
import logging

# Add project root to path
sys.path.append(os.getcwd())

from config.settings import config
from engine.database import sync_trades_from_orders, get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BaselineSanitizer")

def sanitize_baseline():
    logger.info(f"🚀 Starting Operational Baseline Sanitization (v{config.VERSION})")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Fetch all active bots
    active_bots = cursor.execute("SELECT id, name, pair FROM bots WHERE is_active = 1").fetchall()
    
    updated_count = 0
    for bot_id, name, pair in active_bots:
        logger.info(f"Checking Bot {bot_id} ({name} - {pair})...")
        was_updated = sync_trades_from_orders(bot_id)
        if was_updated:
            logger.info(f"✅ Bot {bot_id} was OUT OF SYNC. Fixed via Self-Healing Protocol.")
            updated_count += 1
        else:
            logger.info(f"🟢 Bot {bot_id} is in sync.")
            
    logger.info(f"🏁 Sanitization complete. Updated {updated_count} bots out of {len(active_bots)}.")
    
    # 2. Final verification of Global Netting
    cursor.execute("""
        SELECT b.id, b.name, t.total_invested, t.current_step, t.cycle_phase, b.status
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1 AND (t.total_invested > 0 OR t.cycle_phase != 'IDLE')
    """)
    rows = cursor.fetchall()
    
    print("\n--- POST-SANITIZATION STATUS REPORT ---")
    print(f"{'ID':<6} | {'Name':<20} | {'Invested':<10} | {'Step':<5} | {'Phase':<10} | {'Bot Status'}")
    print("-" * 75)
    for r in rows:
        print(f"{r[0]:<6} | {r[1]:<20} | ${r[2]:<9.2f} | {r[3]:<5} | {r[4]:<10} | {r[5]}")
    print("-" * 75)

if __name__ == "__main__":
    sanitize_baseline()
