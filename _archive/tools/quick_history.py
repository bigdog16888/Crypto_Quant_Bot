
import sqlite3
import os
import sys

sys.path.append(os.getcwd())
from config.settings import config

def check_history():
    db_path = config.PATHS["DB_FILE"]
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("\n--- Recent SYSTEM_FIX Actions ---")
    cursor.execute("SELECT bot_id, action, notes, timestamp FROM trade_history WHERE action='SYSTEM_FIX' ORDER BY timestamp DESC LIMIT 5")
    for r in cursor.fetchall():
        print(f"Bot {r[0]} | Action {r[1]} | Notes {r[2]} | TS {r[3]}")
    
    print("\n--- Bot Mapping ---")
    cursor.execute("SELECT id, name FROM bots WHERE id IN (10000, 10001, 10002)")
    for r in cursor.fetchall():
        print(f"ID {r[0]} = {r[1]}")

    conn.close()

if __name__ == "__main__":
    check_history()
