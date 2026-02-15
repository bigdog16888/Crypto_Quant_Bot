from engine.exchange_interface import ExchangeInterface
import sqlite3
import pandas as pd
import json

def final_health_check_v2():
    print("--- 🩺 FINAL SYSTEM HEALTH CHECK V2 ---")
    
    ex_f = ExchangeInterface(market_type='future')
    conn = sqlite3.connect('crypto_bot.db')
    
    # 1. Fetch Positions
    pos = ex_f.fetch_positions()
    active_pos = {p['symbol']: p for p in pos if float(p.get('contracts', 0)) > 0}
    
    # 2. Fetch DB Trades joined with Bots for symbols
    query = """
    SELECT b.id as bot_id, b.pair as symbol, t.current_step, t.total_invested 
    FROM trades t
    JOIN bots b ON t.bot_id = b.id
    """
    trades = pd.read_sql_query(query, conn)
    
    # 3. Fetch Open Orders
    orders = ex_f.fetch_open_orders()
    
    print("\n[Bot Health Summary]")
    for _, row in trades.iterrows():
        bid, sym, step, invested = row['bot_id'], row['symbol'], row['current_step'], row['total_invested']
        status = "✅ SYNCED"
        issue = ""
        
        # Check Exchange Position
        if sym not in active_pos:
            status = "❌ POSITION MISSING ON EXCHANGE"
        else:
            e_size = float(active_pos[sym]['contracts'])
            # Tolerance for precision
            if abs(e_size * float(active_pos[sym]['entryPrice']) - invested) > 10.0:
                 issue += f" | Invested Mismatch: DB ${invested:.2f} vs EX ${e_size * float(active_pos[sym]['entryPrice']):.2f}"
        
        # Check Orders (Strict matching via ClientOrderID)
        bot_orders = [o for o in orders if o.get('clientOrderId', '').startswith(f"CQB_{bid}_")]
        if not bot_orders:
            status = "⚠️ NO ORDERS FOUND"
        
        print(f" {status} | Bot {bid} ({sym}): Step {step}, Invested ${invested:.2f}{issue}")
        for o in bot_orders:
             print(f"    - Order: {o['clientOrderId']} | {o['side']} {o['amount']} @ {o['price']}")

    print("\n[Exchange Orphans]")
    # Get all names of active pairs from DB to find truly orphaned positions
    db_active_pairs = set(trades['symbol'].values)
    for sym, p in active_pos.items():
        if sym not in db_active_pairs:
            print(f" ⚠️ EXTERNAL POSITION: {sym} | Size: {p['contracts']} | Entry: {p['entryPrice']}")

    conn.close()

if __name__ == "__main__":
    final_health_check_v2()
