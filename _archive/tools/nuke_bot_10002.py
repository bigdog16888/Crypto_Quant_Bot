import sqlite3
import os
import sys

# Add project root
sys.path.append(os.getcwd())
from engine.database import DB_PATH

def nuke_bot_10002():
    print(f"Connecting to DB at {DB_PATH}")
    print(f"Real Path: {os.path.abspath(DB_PATH)}")
    conn = sqlite3.connect(DB_PATH)
    
    # Force Checkpoint
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    
    cursor = conn.cursor()
    
    # 1. Check Bots
    cursor.execute("SELECT id, name, pair FROM bots WHERE id=10002")
    bot = cursor.fetchone()
    print(f"Bot 10002: {bot}")
    
    # 2. Check Trades (Did it come back?)
    cursor.execute("SELECT * FROM trades WHERE bot_id=10002")
    trade = cursor.fetchone()
    print(f"Trade for 10002 (CURRENT): {trade}")
    
    if trade:
        print("DELETE from trades AGAIN...")
        cursor.execute("DELETE FROM trades WHERE bot_id=10002")
        conn.commit()
    
    # 3. Check Left Join View
    cursor.execute("SELECT b.id, COALESCE(t.total_invested, 0) FROM bots b LEFT JOIN trades t ON b.id = t.bot_id WHERE b.id=10002")
    view = cursor.fetchone()
    print(f"JOIN View for 10002: {view}")
    
    conn.close()

if __name__ == "__main__":
    nuke_bot_10002()
