import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()
c.execute("SELECT id FROM bots WHERE pair LIKE '%LINK%'")
bid = c.fetchone()[0]

c.execute("SELECT amount FROM bot_orders WHERE bot_id=? AND status='filled' AND order_type IN ('entry', 'grid') AND created_at >= 1773024135", (bid,))
fills = [r[0] for r in c.fetchall()]
total_qty = sum(fills)

c.execute("SELECT total_invested, avg_entry_price, current_step FROM trades WHERE bot_id=?", (bid,))
inv, entry, step = c.fetchone()

print(f"Fills: {fills}")
print(f"Sum Filled Qty = {total_qty:.2f}")
print(f"Trades DB Qty = {inv/entry if entry > 0 else 0:.2f} (Inv: {inv}, Entry: {entry})")

from engine.exchange_interface import ExchangeInterface
ex = ExchangeInterface()
pos = ex.fetch_positions()
hq = [p['contracts'] for p in pos if 'LINK' in p['symbol']]
print(f"Exchange Qty = {abs(hq[0]) if hq else 0:.2f}")

conn.close()
