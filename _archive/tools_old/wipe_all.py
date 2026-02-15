import sqlite3
import os
import sys

# Add parent to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.database import get_connection

def wipe_all_trades():
    print("🧨 WIPING ALL TRADE STATE (OWNERS & PASSENGERS) 🧨")
    print("==================================================")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Reset Trades Table
    cursor.execute("UPDATE trades SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=0")
    print(f"   ✅ Reset {cursor.rowcount} bots in 'trades' table.")
    
    # 2. Reset Ownership State
    cursor.execute("DELETE FROM bot_ownership_state")
    print(f"   ✅ Cleared {cursor.rowcount} ownership records.")
    
    # 3. Reset Orders Table
    cursor.execute("DELETE FROM bot_orders")
    print(f"   ✅ Cleared {cursor.rowcount} order records.")
    
    # 4. Clear Notifications (Prevent "TP Hit" spam from old events)
    cursor.execute("DELETE FROM notifications")
    print(f"   ✅ Cleared {cursor.rowcount} notifications.")
    
    conn.commit()
    conn.close()
    print("==================================================")

if __name__ == "__main__":
    wipe_all_trades()
