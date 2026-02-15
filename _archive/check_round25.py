
import os
import sys
import time
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

def check_status():
    print("--- ROUND 25 DIAGNOSTIC ---")
    
    # 1. Active Bots
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT b.id, b.name, b.pair, t.current_step, b.status 
        FROM bots b 
        LEFT JOIN trades t ON b.id = t.bot_id 
        WHERE b.is_active=1
    """)
    active_bots = cursor.fetchall()
    print(f"Active Bots: {len(active_bots)}")
    for bot in active_bots:
        print(f" - Bot {bot[0]} ({bot[1]}): {bot[2]} | Step: {bot[3]} | Status: {bot[4]}")
        
    if not active_bots:
        print("NO ACTIVE BOTS.")
        
    # 2. Open Orders (DB)
    print("\n--- OPEN ORDERS (DB) ---")
    cursor.execute("SELECT bot_id, order_type, price, amount, order_id FROM bot_orders WHERE status='open'")
    db_orders = cursor.fetchall()
    print(f"Total Open Orders in DB: {len(db_orders)}")
    for order in db_orders:
        print(f" - Bot {order[0]} [{order[1]}]: {order[3]} @ {order[2]} (ID: {order[4]})")
        
    # 3. Exchange Positions
    print("\n--- EXCHANGE POSITIONS ---")
    try:
        ex = ExchangeInterface(market_type='future')
        # Iterate allowed symbols to find positions if get_all doesn't work
        positions = []
        # Try raw exchange fetch if available, else standard method
        if hasattr(ex.exchange, 'fetch_positions'):
            positions = ex.exchange.fetch_positions()
        else:
            # Fallback
            positions = ex.get_all_positions() 
            
        valid_positions = [p for p in positions if float(p['info']['positionAmt']) != 0] # Binance specific
        print(f"Total Exchange Positions: {len(valid_positions)}")
        for p in valid_positions:
            amt = float(p['info']['positionAmt'])
            print(f" - {p['symbol']}: {amt} @ {p['entryPrice']}")
    except Exception as e:
        print(f"Error fetching positions: {e}")

    # 4. Exchange Orders (for Bots)
    print("\n--- EXCHANGE ORDERS ---")
    for bot in active_bots:
        pair = bot[2]
        try:
            orders = ex.get_open_orders(pair)
            print(f"Orders for {pair}: {len(orders)}")
            for o in orders:
                print(f" - {o['type']} {o['side']} {o['amount']} @ {o['price']} (ID: {o['id']})")
        except Exception as e:
            print(f"Error fetching orders for {pair}: {e}")

    conn.close()

if __name__ == "__main__":
    check_status()
