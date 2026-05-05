import sqlite3
import json
import os

DB_PATH = "crypto_bot.db"

def find_bot():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Find the BTC short bot
    cursor.execute("SELECT * FROM bots WHERE name LIKE '%short btc%'")
    bots = [dict(row) for row in cursor.fetchall()]
    
    for b in bots:
        bid = b['id']
        cursor.execute("SELECT * FROM trades WHERE bot_id=?", (bid,))
        b['trade'] = dict(cursor.fetchone()) if cursor.rowcount != 0 else None
        
        # Check for hedge orders
        cursor.execute("SELECT * FROM bot_orders WHERE bot_id=? AND order_type='hedge' ORDER BY id DESC", (bid,))
        b['hedge_orders'] = [dict(row) for row in cursor.fetchall()]
        
        # Check for hedge_tp orders
        cursor.execute("SELECT * FROM bot_orders WHERE bot_id=? AND order_type='hedge_tp' ORDER BY id DESC", (bid,))
        b['hedge_tp_orders'] = [dict(row) for row in cursor.fetchall()]
        
        # Check for all other orders in current cycle
        cycle_id = b['trade']['cycle_id'] if b['trade'] else 1
        cursor.execute("SELECT * FROM bot_orders WHERE bot_id=? AND cycle_id=? ORDER BY id DESC", (bid, cycle_id))
        b['cycle_orders'] = [dict(row) for row in cursor.fetchall()]

    print(json.dumps(bots, indent=2))
    conn.close()

if __name__ == "__main__":
    find_bot()
