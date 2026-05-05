import sqlite3
import json
import os

DB_PATH = "crypto_bot.db"

def list_bots():
    if not os.path.exists(DB_PATH):
        print(f"Error: {DB_PATH} not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, name, pair, status, is_active FROM bots")
    bots = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM trades")
    trades = [dict(row) for row in cursor.fetchall()]
    
    cursor.execute("SELECT * FROM active_positions")
    positions = [dict(row) for row in cursor.fetchall()]

    print(json.dumps({
        "bots": bots,
        "trades": trades,
        "positions": positions
    }, indent=2))
    conn.close()

if __name__ == "__main__":
    list_bots()
