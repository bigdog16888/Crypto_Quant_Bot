import sqlite3
import os
import time

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def fix_ghost_10002():
    print(f"--- FIXING GHOST BOT 10002 (Gold) ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 1. Clear Active Trade
        print("1. Clearing Trade Record...")
        cursor.execute("DELETE FROM trades WHERE bot_id = 10002")
        
        # 2. Reset Bot Status
        print("2. Resetting Bot to Scanning...")
        cursor.execute("UPDATE bots SET is_active = 1, status = 'Scanning' WHERE id = 10002")
        
        conn.commit()
        conn.close()
        print("✅ Bot 10002 Reset Complete.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    fix_ghost_10002()
