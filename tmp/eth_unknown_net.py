import sqlite3
import sys, os, time
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from engine.exchange_interface import ExchangeInterface

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Get ALL known OIDs across ALL ETH bots
c.execute("SELECT DISTINCT order_id FROM bot_orders WHERE bot_id IN (10011, 10021, 100002) AND order_id IS NOT NULL")
known_oids = {str(r[0]) for r in c.fetchall()}
print(f"Known OIDs: {len(known_oids)}")

conn.close()

ex = ExchangeInterface('future')
since = int((time.time() - 86400 * 3) * 1000)
trades = ex.fetch_my_trades('ETHUSDC', since=since, limit=500)

# Net of UNKNOWN fills only
unknown_buy = 0.0
unknown_sell = 0.0
from collections import defaultdict
oid_qty = defaultdict(float)
oid_side = {}

for t in trades:
    oid = str(t.get('order', ''))
    qty = float(t.get('amount', 0))
    side = t.get('side', '').lower()
    oid_qty[oid] += qty
    oid_side[oid] = side

for oid, qty in oid_qty.items():
    if oid not in known_oids:
        if oid_side[oid] == 'buy':
            unknown_buy += qty
        else:
            unknown_sell += qty

print(f"Unknown fills: buy={unknown_buy:.4f}, sell={unknown_sell:.4f}")
print(f"Unknown NET (buy-sell for SHORT perspective): {unknown_buy - unknown_sell:.6f}")
print("(negative = net SHORT from untracked trades)")
