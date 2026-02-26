
import sqlite3
import os
import sys

sys.path.append(os.getcwd())
from config.settings import config

def manual_ghost_bust(bot_id):
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print(f"--- Manual Ghost Bust for Bot {bot_id} ---")
    
    # 1. Reset Trade State
    cursor.execute("""
        UPDATE trades 
        SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=0 
        WHERE bot_id=?
    """, (bot_id,))
    
    # 2. Reset Bot Status
    cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
    
    # 3. Log
    try:
        from engine.database import log_trade
        log_trade(bot_id, 'SYSTEM_FIX', 'XAU/USDT', 0, 0, 0, f"MANUAL_FIX_{int(time.time())}", 0, "Manual Ghost Bust via Script", 0)
    except:
        print("Could not log to history via module, skipping.")

    conn.commit()
    print(f"Bot {bot_id} reset to Scanning/Invested=0.")
    conn.close()

if __name__ == "__main__":
    import time
    if len(sys.argv) > 1:
        target_bot_id = int(sys.argv[1])
        manual_ghost_bust(target_bot_id)
    else:
        print("Usage: python tools/manual_fix_ghost.py <BOT_ID>")
