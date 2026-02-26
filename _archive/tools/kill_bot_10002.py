import sqlite3
import os
import sys

# Add project root
sys.path.append(os.getcwd())
from engine.database import DB_PATH

def kill_bot_10002():
    print(f"Connecting to DB at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cursor = conn.cursor()
    
    # 1. Deactivate Bot 10002
    print("Deactivating Bot 10002...")
    cursor.execute("UPDATE bots SET is_active=0, status='Stopped' WHERE id=10002")
    
    # 2. Delete Trade
    print("Deleting Trade for 10002...")
    cursor.execute("DELETE FROM trades WHERE bot_id=10002")
    
    conn.commit()
    
    # Verify
    cursor.execute("SELECT is_active FROM bots WHERE id=10002")
    active = cursor.fetchone()[0]
    cursor.execute("SELECT total_invested FROM trades WHERE bot_id=10002")
    trade = cursor.fetchone()
    
    print(f"Bot 10002 Active: {active}")
    print(f"Bot 10002 Trade: {trade}")
    
    conn.close()

if __name__ == "__main__":
    kill_bot_10002()
