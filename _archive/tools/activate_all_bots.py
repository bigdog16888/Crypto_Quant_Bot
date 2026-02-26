
import sqlite3
import os

DB_PATH = 'crypto_bot.db'

def activate_all_bots():
    print("--- ACTIVATING ALL BOTS ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # 1. Update Activation
        cursor.execute("UPDATE bots SET is_active=1, status='Scanning' WHERE id BETWEEN 10000 AND 10020") # Safe range
        
        # 2. Verify
        cursor.execute("SELECT id, name, pair FROM bots WHERE is_active=1")
        active = cursor.fetchall()
        
        conn.commit()
        conn.close()
        
        print(f"✅ Successfully activated {len(active)} bots:")
        for b in active:
            print(f"   - [{b[0]}] {b[1]} ({b[2]})")
            
    except Exception as e:
        print(f"❌ Failed to activate bots: {e}")

if __name__ == "__main__":
    activate_all_bots()
