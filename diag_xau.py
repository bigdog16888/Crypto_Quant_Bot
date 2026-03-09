import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
c = conn.cursor()

c.execute("SELECT b.id, b.name, b.direction, t.total_invested, t.avg_entry_price, t.current_step FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%XAU%'")
xau_bots = c.fetchall()
system_qty = 0
for r in xau_bots:
    qty = (r[3] / r[4]) if r[4] > 0 else 0
    print(f"Bot {r[0]} ({r[1]}): Dir={r[2]}, Inv={r[3]:.2f}, Entry={r[4]:.4f}, Step={r[5]}, CalcQty={qty:.4f}")
    if r[2].upper() == 'LONG': system_qty += qty
    else: system_qty -= qty

ex = ExchangeInterface()
pos = ex.fetch_positions()
ex_qty = 0
for p in pos:
    if 'XAU' in p['symbol']:
        ex_qty = p['contracts']
        if p['side'].upper() == 'SHORT': ex_qty = -abs(ex_qty)
        else: ex_qty = abs(ex_qty)
        print(f"Exchange {p['symbol']}: Qty={ex_qty}")

print(f"\nNet System Qty: {system_qty:.4f}")
print(f"Net Exch Qty  : {ex_qty:.4f}")
print(f"Diff          : {system_qty - ex_qty:.4f} contracts")

conn.close()
