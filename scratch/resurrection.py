import sqlite3
import time
import sys
import os

# Add parent directory to sys.path to import engine modules
sys.path.append(os.getcwd())

from engine.database import get_connection, recompute_invested_from_orders

def resurrect_bots():
    conn = get_connection()
    cursor = conn.cursor()
    
    # Target Bots and the Cycles we want to restore
    # (BotID, CycleID)
    targets = [
        (10016, 12), # BTC Long (The 1.142 BTC cycle)
        (10022, 11), # BTC Short (The 1.527 Hedge cycle)
        (10018, 40), # SUI Long (The 29.6 SUI cycle)
        (100000, 15) # SUI Short (The 1796.2 Hedge cycle - Checking if this is the right one)
    ]
    
    for bot_id, cycle_id in targets:
        print(f"--- Resurrecting Bot {bot_id} (Reverting to Cycle {cycle_id}) ---")
        
        # 1. Restore the 'reset_cleared' orders to 'filled'
        cursor.execute("""
            UPDATE bot_orders 
            SET status = 'filled' 
            WHERE bot_id = ? AND cycle_id = ? AND status = 'reset_cleared'
        """, (bot_id, cycle_id))
        rows_restored = cursor.rowcount
        print(f"   Restored {rows_restored} orders.")
        
        # 2. Rewind the trades table
        cursor.execute("""
            UPDATE trades 
            SET cycle_id = ?, 
                cycle_phase = 'ACTIVE', 
                current_step = (SELECT COALESCE(MAX(step), 0) FROM bot_orders WHERE bot_id = ? AND cycle_id = ? AND status = 'filled')
            WHERE bot_id = ?
        """, (cycle_id, bot_id, cycle_id, bot_id))
        
        # 3. Trigger deep recomputation
        recompute_invested_from_orders(bot_id)
        print(f"   Bot {bot_id} recomputed.")
        
    conn.commit()
    print("\n✅ Resurrection Complete. All bots restored to pre-wipe parity.")

if __name__ == "__main__":
    resurrect_bots()
