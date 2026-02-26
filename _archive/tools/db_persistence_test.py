import sqlite3
import os
import time

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def test_persistence():
    print(f"--- DB PERSISTENCE TEST ---")
    print(f"CWD: {os.getcwd()}")
    print(f"DB Path: {DB_PATH}")
    
    if not os.path.exists(DB_PATH):
        print("❌ DB FILE DOES NOT EXIST!")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 1. Check current count
        cursor.execute("SELECT count(*) FROM active_positions")
        initial_count = cursor.fetchone()[0]
        print(f"Initial Count: {initial_count}")
        
        # 2. Insert Test Row
        print("Inserting Test Row...")
        cursor.execute("INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) VALUES (99999, 'TEST/USD', 'LONG', 1.0, 100.0, 1234567890)")
        conn.commit()
        
        # 3. Verify
        cursor.execute("SELECT count(*) FROM active_positions WHERE bot_id=99999")
        test_count = cursor.fetchone()[0]
        if test_count == 1:
            print("✅ Insert Verified.")
        else:
            print("❌ Insert Failed!")
            
        # 4. Clean up
        print("Cleaning up...")
        cursor.execute("DELETE FROM active_positions WHERE bot_id=99999")
        conn.commit()
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    test_persistence()
