import sqlite3
import os
import sys

# Add parent dir to path
sys.path.append(os.getcwd())
try:
    from engine.database import get_connection
except:
    print("Import failed, using simplified checking")

def fix_ghost_10000():
    print("--- FIXING GHOST BOT 10000 ---", flush=True)
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 1. Verify it's still messed up
        cursor.execute("SELECT total_invested FROM trades WHERE bot_id=10000")
        row = cursor.fetchone()
        if not row:
            print("  Bot 10000 has no trade record. Already fixed?")
            return

        print(f"  Current Invested: {row[0]}")
        
        # 2. CLEAR IT
        print("  Resetting Trade Record...", flush=True)
        cursor.execute("""
            UPDATE trades 
            SET total_invested=0, current_step=0, entry_order_id=NULL, tp_order_id=NULL, entry_confirmed=0
            WHERE bot_id=10000
        """)
        
        print("  Resetting Bot Status...", flush=True)
        cursor.execute("UPDATE bots SET status='Scanning' WHERE id=10000")
        
        # 3. Clear Grid Orders
        print("  Clearing Ghost Grid Orders...", flush=True)
        cursor.execute("DELETE FROM bot_orders WHERE bot_id=10000")
        
        conn.commit()
        print("  ✅ FIXED. Bot 10000 should now be Scanning.")
        
    except Exception as e:
        print(f"  ❌ FAILED: {e}")

if __name__ == "__main__":
    fix_ghost_10000()
