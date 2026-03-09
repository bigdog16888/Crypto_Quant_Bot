import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

ex = ExchangeInterface()
all_orders = []
pairs = ['BTC/USDC:USDC', 'LINK/USDC:USDC', 'SOL/USDC:USDC', 'SUI/USDC:USDC', 'XRP/USDC:USDC', 'XAU/USDT:USDT']
for sym in pairs:
    try:
        all_orders.extend(ex.fetch_open_orders(sym))
    except Exception as e:
        print("Error fetching", sym, e)

conn = get_connection()
c = conn.cursor()
c.execute("SELECT order_id FROM bot_orders WHERE status='open'")
db_orders = {r[0] for r in c.fetchall()}

print(f"Total Exchange Orders: {len(all_orders)}")
print(f"Total DB Open Orders: {len(db_orders)}")

for o in all_orders:
    if str(o['id']) not in db_orders:
        print(f"STRAY ORDER: {o['id']} {o.get('symbol')} {o.get('side')} {o.get('amount')} @ {o.get('price')} clientID={o.get('clientOrderId')}")

conn.close()
