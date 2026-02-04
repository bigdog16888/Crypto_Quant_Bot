
import sqlite3
import json
import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute('SELECT id, name, pair, direction FROM bots WHERE is_active=1')
active_bots = cursor.fetchall()

print(f"--- BOT STATUS AUDIT ({len(active_bots)} Active Bots) ---")
in_trade_count = 0
for b_id, name, pair, direction in active_bots:
    cursor.execute('SELECT current_step, total_invested FROM trades WHERE bot_id=?', (b_id,))
    trade = cursor.fetchone()
    if trade and trade[1] > 0:
        in_trade_count += 1
        print(f"Bot {b_id} ({name}): IN TRADE (Step {trade[0]}, Invested ${trade[1]:.2f})")
    else:
        print(f"Bot {b_id} ({name}): IDLE")

ex = ExchangeInterface(market_type='future')
orders = ex.exchange.fetch_open_orders()
print(f"\n--- EXCHANGE ORDER AUDIT ({len(orders)} Total Orders) ---")

bot_order_map = {}
unknown_orders = []

for o in orders:
    client_id = o.get('clientOrderId', 'NONE')
    if client_id.startswith('CQB_'):
        # Format: CQB_{bot_id}_{type}_{uuid}
        parts = client_id.split('_')
        if len(parts) >= 2:
            try:
                b_id = int(parts[1])
                if b_id not in bot_order_map: bot_order_map[b_id] = []
                bot_order_map[b_id].append(o)
            except:
                unknown_orders.append(o)
        else:
            unknown_orders.append(o)
    else:
        unknown_orders.append(o)

for b_id, name, pair, direction in active_bots:
    my_orders = bot_order_map.get(b_id, [])
    print(f"Bot {b_id} ({name}): {len(my_orders)} orders on exchange")
    for o in my_orders:
        print(f"   - {o['side']} {o['type']} @ {o['price']} (ID: {o['clientOrderId']})")

if unknown_orders:
    print(f"\n--- UNKNOWN/UNTAGGED ORDERS ({len(unknown_orders)}) ---")
    for o in unknown_orders:
         print(f"   - {o['symbol']} {o['side']} @ {o['price']} (ID: {o['clientOrderId']})")

conn.close()
