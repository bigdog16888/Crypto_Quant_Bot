import sqlite3
import os
import time

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def test_write():
    print(f"--- TESTING WRITE to {DB_PATH} ---")
    try:
        conn = sqlite3.connect(DB_PATH, isolation_level=None)
        cursor = conn.cursor()
        
        print("1. Deleting existing...")
        cursor.execute("DELETE FROM active_positions")
        
        print("2. Inserting Test Row...")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
            VALUES (0, 'TEST/USD', 'LONG', 1.0, 50000.0, ?)
        """, (int(time.time()),))
        cursor.execute("COMMIT")
        
        print("3. Reading back...")
        cursor.execute("SELECT * FROM active_positions")
        rows = cursor.fetchall()
        print(f"Rows found: {len(rows)}")
        print(rows)
        
        if len(rows) > 0:
            print("✅ WRITE SUCCESS")
        else:
            print("❌ WRITE FAILED")

        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_write()
