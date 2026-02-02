import sqlite3
import os
import json
import logging
import sys

# Add current directory to path
sys.path.append(os.getcwd())

from engine.database import DB_PATH, get_connection
from engine.exchange_interface import ExchangeInterface

def diagnostic():
    print("--- BOT DIAGNOSTIC REPORT ---")
    
    # 1. DB Check
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, name, pair, direction, status, is_active FROM bots")
    bots = cursor.fetchall()
    
    print(f"\nTotal Bots: {len(bots)}")
    active_bots = [b for b in bots if b[5]]
    print(f"Active (is_active=1) Bots: {len(active_bots)}")
    
    cursor.execute("""
        SELECT b.id, b.name, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price, t.target_tp_price 
        FROM bots b 
        JOIN trades t ON b.id = t.bot_id 
        WHERE t.total_invested > 0
    """)
    trades = cursor.fetchall()
    
    print(f"Bots in Trade (total_invested > 0): {len(trades)}")
    
    for t in trades:
        bid, name, pair, side, step, invested, entry, tp = t
        print(f"\n[BOT: {name} | {pair} | {side}]")
        print(f"  Step: {step}")
        print(f"  Invested: ${invested:.2f}")
        print(f"  Avg Entry: {entry:.4f}")
        print(f"  Target TP: {tp:.4f}")
        
        # Calculate expected NO price (roughly)
        # Note: This depends on the strategy, but usually it's below entry
        # We can also check the bot_orders table
        cursor.execute("SELECT order_type, price, amount, status FROM bot_orders WHERE bot_id = ? AND status = 'open'", (bid,))
        orders = cursor.fetchall()
        print(f"  Open Orders in DB: {len(orders)}")
        for o in orders:
            print(f"    - {o[0]}: {o[2]:.4f} @ {o[1]:.4f}")

    # 2. Exchange Check
    try:
        ex = ExchangeInterface(market_type='future')
        print("\n--- EXCHANGE STATUS ---")
        positions = ex.fetch_positions()
        open_positions = []
        for p in positions:
            if not p: continue
            # Handle both 'size' and 'contracts'
            size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if size != 0:
                open_positions.append(p)
        
        print(f"Open Positions on Exchange: {len(open_positions)}")
        for p in open_positions:
            print(f"  - {p.get('symbol')}: {p.get('contracts') or p.get('size')} @ {p.get('entryPrice')}")
            
        all_open_orders = []
        unique_pairs = set(b[2] for b in bots if b[2])
        for pair in unique_pairs:
            try:
                orders = ex.fetch_open_orders(pair)
                if orders:
                    all_open_orders.extend(orders)
            except:
                pass
        
        print(f"Total Open Orders on Exchange: {len(all_open_orders)}")
        for o in all_open_orders:
            print(f"  - {o.get('symbol')} | {o.get('side')} | {o.get('type')} | {o.get('amount')} @ {o.get('price')}")
            
    except Exception as e:
        import traceback
        print(f"\nError checking exchange: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    diagnostic()
