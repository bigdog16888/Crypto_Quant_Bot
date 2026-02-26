import sqlite3
import os
import sys

# Add project root
sys.path.append(os.getcwd())
from engine.database import DB_PATH

def reset_btc_bots():
    print(f"Connecting to DB at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Reset trades for ALL bots on BTC pairs
        print("Resetting trades for ALL bots on BTC pairs...")
        cursor.execute("UPDATE trades SET total_invested=0, current_step=0, entry_confirmed=0 WHERE bot_id IN (SELECT id FROM bots WHERE pair LIKE '%BTC%')")
        print(f"Reset {cursor.rowcount} trades.")
        conn.commit()
    except Exception as e:
        print(f"Error: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    reset_btc_bots()
