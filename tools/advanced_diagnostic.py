import sqlite3
import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

# Disable excessive logging
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

def get_db_data():
    conn = sqlite3.connect('crypto_bot.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get active bots in trade
    cursor.execute("SELECT t.*, b.name, b.pair, b.config FROM trades t JOIN bots b ON t.bot_id = b.id")
    trades = [dict(row) for row in cursor.fetchall()]
    
    # Get all tracked open orders
    cursor.execute("SELECT * FROM bot_orders WHERE status = 'open'")
    tracked_orders = [dict(row) for row in cursor.fetchall()]
    
    return trades, tracked_orders

def run_report():
    print("--- ADVANCED ORDER ATTRIBUTION REPORT ---")
    trades, tracked_orders = get_db_data()
    ex = ExchangeInterface(market_type='future')
    
    # 1. Fetch all open orders from exchange for relevant pairs
    pairs = set(t['pair'] for t in trades)
    exchange_orders = []
    for pair in pairs:
        try:
            orders = ex.fetch_open_orders(pair)
            if orders:
                exchange_orders.extend(orders)
        except Exception as e:
            print(f"⚠️ Error fetching orders for {pair}: {e}")
    
    print(f"Total Bots in Trade: {len(trades)}")
    print(f"Exchange Orders Found: {len(exchange_orders)}")
    print("="*50)

    # 2. Match exchange orders to bots
    for t in trades:
        print(f"\n[BOT: {t['name']} | {t['pair']}]")
        print(f"  Avg Entry: {t['avg_entry_price']:.4f} | Target TP (DB): {t['target_tp_price']:.4f}")
        
        # Internal Tracked IDs
        bot_tracked = [to for to in tracked_orders if to['bot_id'] == t['bot_id']]
        tracked_ids = [to['order_id'] for to in bot_tracked]
        
        # Find matching exchange orders
        found_on_exchange = [o for o in exchange_orders if o['id'] in tracked_ids]
        
        print(f"  Tracked Orders in DB: {len(bot_tracked)}")
        for to in bot_tracked:
            on_ex = "YES" if to['order_id'] in [o['id'] for o in found_on_exchange] else "MISSING"
            print(f"    - {to['order_type']} {to['amount']} @ {to['price']} | ID: {to['order_id']} | Exchange: {on_ex}")
            
        # Check for unowned orders on this pair that might belong to this bot (by size/price similarity)
        # Note: This is an estimation for troubleshooting
        unowned = [o for o in exchange_orders if normalize_symbol(o['symbol']) == normalize_symbol(t['pair']) and o['id'] not in tracked_ids]
        if unowned:
            # Check if any unknown orders match this bot's trade size
            for u in unowned:
                if abs(float(u['amount']) - (t['total_invested']/t['avg_entry_price'])) < 0.0001:
                    print(f"    ⚠️ POTENTIAL UNTRACKED TP: {u['amount']} @ {u['price']} | ID: {u['id']}")

    print("\n" + "="*50)
    print("UNCATEGORIZED EXCHANGE ORDERS:")
    all_tracked_ids = [to['order_id'] for to in tracked_orders]
    for o in exchange_orders:
        if o['id'] not in all_tracked_ids:
            print(f"  - {o['symbol']} | {o['side']} | {o['amount']} @ {o['price']} | ID: {o['id']}")

if __name__ == "__main__":
    run_report()
