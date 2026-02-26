import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.database import get_connection

def output_status():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT bot_id, last_exit_time, total_invested, basket_start_time, entry_confirmed FROM trades WHERE bot_id=10000")
    print(f"BEFORE: {c.fetchone()}")
    
    # Force Reset Last Exit Time AND Ensure Entry Confirmed to stop Reconciler loop?
    # Actually, if we want it to trigger NEW entry, we want entry_confirmed=0 and Invested=0.
    # But Reconciler keeps finding a fill? 
    # Let's just clear last_exit for now.
    # FINAL UNBLOCK
    # Reconciler has caught up. History is in DB.
    # Just clear the timestamp so it can trade.
    c.execute("UPDATE trades SET last_exit_time=0 WHERE bot_id=10000")
    conn.commit()
    
    c.execute("SELECT bot_id, last_exit_time, total_invested, basket_start_time, entry_confirmed FROM trades WHERE bot_id=10000")
    print(f"AFTER:  {c.fetchone()}")
    
if __name__ == "__main__":
    output_status()
