import sqlite3
import os
import sys

# Add project root
sys.path.append(os.getcwd())
from engine.database import DB_PATH

def dump_btc_bots():
    print(f"Connecting to DB at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check Bots on BTC/USDC (fuzzy match)
    cursor.execute("SELECT id, name, pair, direction, is_active, status, strategy_type FROM bots WHERE pair LIKE '%BTC%'")
    bots = cursor.fetchall()
    print("\n--- BTC BOTS ---")
    for b in bots:
        print(f"Bot: {b}")
        # Check Trade State
        cursor.execute("SELECT * FROM trades WHERE bot_id = ?", (b[0],))
        t = cursor.fetchone()
        print(f"   Trade: {t}")

    conn.close()

if __name__ == "__main__":
    dump_btc_bots()
