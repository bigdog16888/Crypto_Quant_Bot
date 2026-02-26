import sqlite3
import time

def test_query():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    # Check all bots
    cursor.execute("SELECT id, name FROM bots WHERE status='In Trade' OR status='Active'")
    bots = cursor.fetchall()
    
    for bot_id, name in bots:
        # Get trades state
        cursor.execute("SELECT current_step, basket_start_time FROM trades WHERE bot_id=?", (bot_id,))
        trade = cursor.fetchone()
        if not trade: continue
        curr_step, basket_start = trade
        
        # EXACT query from bot_executor.py
        query = "SELECT COUNT(*) FROM bot_orders WHERE bot_id=? AND status='filled' AND step=? AND created_at >= ?"
        
        # Test original
        cursor.execute(query, (bot_id, curr_step, basket_start))
        c_orig = cursor.fetchone()[0]
        
        # Test with -60
        cursor.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id=? AND status='filled' AND step=? AND created_at >= (? - 60)", (bot_id, curr_step, basket_start))
        c_new = cursor.fetchone()[0]
        
        # Look at actual row
        cursor.execute("SELECT created_at, status FROM bot_orders WHERE bot_id=? AND step=?", (bot_id, curr_step))
        rows = cursor.fetchall()
        
        print(f"Bot {bot_id} Step={curr_step} Basket={basket_start}")
        print(f"  OrigCount: {c_orig}, NewCount: {c_new}")
        print(f"  Actual Rows: {rows}")
        
test_query()
