import sqlite3
import os
import time

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def check_db():
    print(f"--- CHECKING {DB_PATH} ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM active_positions")
        rows = cursor.fetchall()
        print(f"Found {len(rows)} rows:")
        for r in rows:
            print(r)
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_db()
