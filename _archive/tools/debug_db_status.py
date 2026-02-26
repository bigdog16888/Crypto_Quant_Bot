
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config.settings import config

def check_specific_db(db_path, label):
    print(f"\n====== Checking {label} at: {db_path} ======")
    
    if not os.path.exists(db_path):
        print(f"❌ {label} file NOT FOUND!")
        return

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # 1. Check Bots
        try:
            print(f"--- BOTS TABLE ({label}) ---")
            cur.execute("SELECT count(*) FROM bots")
            count = cur.fetchone()[0]
            print(f"Total Bots: {count}")
            
            cur.execute("SELECT id, name, pair, is_active, status FROM bots")
            rows = cur.fetchall()
            for r in rows:
                print(f"  [{r[0]}] {r[1]} ({r[2]}) - Active: {r[3]}, Status: {r[4]}")
        except Exception as e:
            print(f"  (Error reading bots: {e})")

        # 2. Check Trades
        try:
            print(f"--- TRADES TABLE ({label}) ---")
            cur.execute("SELECT bot_id, total_invested, current_step FROM trades WHERE total_invested > 0")
            rows = cur.fetchall()
            if not rows:
                print("  (No active trades found)")
            for r in rows:
                print(f"  Bot {r[0]}: Invested=${r[1]}, Step={r[2]}")
        except Exception as e:
             print(f"  (Error reading trades: {e})")

        conn.close()
        
    except Exception as e:
        print(f"❌ DB Error: {e}")

if __name__ == "__main__":
    # Check current configured DB
    check_specific_db(config.PATHS['DB_FILE'], "CURRENT_CONFIGURED_DB")
    
    # Check potential backup/old DBs
    base_dir = config.ROOT_DIR
    check_specific_db(os.path.join(base_dir, "crypto_quant.db"), "POTENTIAL_OLD_DB (crypto_quant.db)")
    check_specific_db(os.path.join(base_dir, "engine", "trades.db"), "POTENTIAL_OLD_DB (engine/trades.db)")
