import sqlite3
import json
from engine.exchange_interface import ExchangeInterface
from engine.database import get_bot_order_ids
from config.settings import config

def fix_duplicates():
    print("Fetching active bots and known order IDs from DB...")
    conn = sqlite3.connect(config.PATHS['DB_FILE'])
    cursor = conn.cursor()
    cursor.execute("SELECT id, pair FROM bots WHERE is_active = 1")
    active_bots = cursor.fetchall()
    conn.close()

    known_order_ids = set()
    for (bot_id, _) in active_bots:
        orders = get_bot_order_ids(bot_id)
        if orders['entry_order_id']: known_order_ids.add(str(orders['entry_order_id']))
        if orders['tp_order_id']: known_order_ids.add(str(orders['tp_order_id']))
        for go in orders.get('grid_orders', []):
            if go.get('order_id'): known_order_ids.add(str(go['order_id']))

    print(f"DB known active orders: {known_order_ids}")
    
    ex = ExchangeInterface(market_type=config.MARKET_TYPE)
    
    active_pairs = set([b[1] for b in active_bots])
    for pair in active_pairs:
        print(f"\nChecking orders for {pair}...")
        try:
            orders = ex.fetch_open_orders(pair)
            bot_orders = [o for o in orders if o.get('clientOrderId', '').startswith('CQB_')]
            
            print(f"Found {len(bot_orders)} Bot orders on Exchange for {pair}.")
            
            for o in bot_orders:
                oid = str(o['id'])
                if oid not in known_order_ids:
                    print(f"⚠️ Cancelling Orphan/Duplicate Order: {oid} ({o['side']} {o['type']} {o['amount']} @ {o['price']})")
                    try:
                        ex.cancel_order(oid, pair)
                        print("✅ Cancelled.")
                    except Exception as e:
                        print(f"❌ Failed to cancel {oid}: {e}")
                else:
                    print(f"✅ Order {oid} is tracked by DB.")
        except Exception as e:
            print(f"Error checking {pair}: {e}")

if __name__ == "__main__":
    fix_duplicates()
