import sqlite3
import json

def inspect_xrp_hedge():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    # 1. Get bot info
    cur.execute("SELECT id, name, pair, direction, is_active, config, status, bot_type, parent_bot_id, hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id = 100313")
    bot = cur.fetchone()
    if bot:
        print("Bot 100313:")
        print(f"  Name: {bot[1]}")
        print(f"  Pair: {bot[2]}")
        print(f"  Direction: {bot[3]}")
        print(f"  Is Active: {bot[4]}")
        print(f"  Status: {bot[6]}")
        print(f"  Bot Type: {bot[7]}")
        print(f"  Parent Bot ID: {bot[8]}")
        print(f"  Hedge Child Bot ID: {bot[9]}")
        print(f"  Hedge Trigger Step: {bot[10]}")
        try:
            cfg = json.loads(bot[5])
            print(f"  Config: {cfg}")
        except:
            print(f"  Config (raw): {bot[5]}")
    else:
        print("Bot 100313 not found!")
        
    # 2. Get trades info
    cur.execute("SELECT * FROM trades WHERE bot_id = 100313")
    trade = cur.fetchone()
    if trade:
        # Get column names
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        print("\nTrade Info:")
        for col, val in zip(cols, trade):
            print(f"  {col}: {val}")
            
    # 3. Get open/active orders in bot_orders
    cur.execute("""
        SELECT id, order_id, order_type, price, amount, filled_amount, status, step, cycle_id, created_at 
        FROM bot_orders 
        WHERE bot_id = 100313 
          AND status NOT IN ('reset_cleared','cancelled','canceled','failed')
        ORDER BY created_at DESC
    """)
    orders = cur.fetchall()
    print(f"\nActive/Filled Orders ({len(orders)} total):")
    for o in orders[:15]:
        print(f"  ID: {o[0]} | OID: {o[1]} | Type: {o[2]} | Price: {o[3]} | Qty: {o[4]} | Filled: {o[5]} | Status: {o[6]} | Step: {o[7]} | CycleID: {o[8]}")
        
    conn.close()

if __name__ == '__main__':
    inspect_xrp_hedge()
