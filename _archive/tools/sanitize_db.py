import sqlite3
import os
import time

def sanitize():
    db_path = "crypto_bot.db"
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.")
        return

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    print("--- 🛡️ Starting Database Sanitization ---")
    
    # 1. Look for invalid entry prices (the $9M PnL suspect)
    # Floor of $10 for BTC/XAU
    c.execute("SELECT bot_id, avg_entry_price, total_invested FROM trades WHERE avg_entry_price > 0 AND avg_entry_price < 10.0")
    bad_prices = c.fetchall()
    
    if bad_prices:
        for bid, price, inv in bad_prices:
            print(f"⚠️ Corrupted data found for Bot {bid}: Price={price}, Invested={inv}. Resetting to IDLE.")
            # Set to step 0 and 0 invested
            c.execute("UPDATE trades SET current_step=0, total_invested=0, avg_entry_price=0, entry_confirmed=0 WHERE bot_id=?", (bid,))
            c.execute("UPDATE bots SET status='Waiting for Signal' WHERE id=?", (bid,))
    else:
        print("✅ No corrupted entry prices found (< $10).")

    # 2. Sync bots table with trades table status
    # If trades say invested > 0 but bots say "Scanning" or "Waiting for Signal", force "IN TRADE"
    c.execute("""
        UPDATE bots 
        SET status = '🟢 IN TRADE' 
        WHERE id IN (SELECT bot_id FROM trades WHERE total_invested > 0)
        AND (status LIKE '%SCANNING%' OR status LIKE '%Waiting%')
    """)
    if c.rowcount > 0:
        print(f"✅ Synchronized {c.rowcount} bots found 'IN TRADE' in the trades table.")

    conn.commit()
    conn.close()
    print("--- ✅ Sanitization Complete ---")

if __name__ == "__main__":
    sanitize()
