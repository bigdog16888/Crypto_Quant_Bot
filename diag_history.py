import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

print("--- 1. XRP Loss Check ---")
c.execute("PRAGMA table_info(trade_history)")
print("Columns:", [r[1] for r in c.fetchall()])

c.execute("SELECT * FROM trade_history WHERE bot_id=10017 ORDER BY timestamp DESC LIMIT 3")
print("XRP History:")
for r in c.fetchall():
    print(r)

print("\n--- 2. LINK Bots & Diff ---")
c.execute("SELECT b.id, b.name, t.total_invested, t.avg_entry_price, t.current_step FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.pair LIKE '%LINK%'")
link_bots = c.fetchall()
system_link_qty = 0
for r in link_bots:
    print(f"Bot {r[0]} ({r[1]}): Inv={r[2]:.2f}, Entry={r[3]:.4f}, Step={r[4]}")
    system_link_qty += (r[2]/r[3]) if r[3]>0 else 0

from engine.exchange_interface import ExchangeInterface
ex = ExchangeInterface()
pos = ex.fetch_positions()
ex_link_qty = 0
for p in pos:
    if 'LINK' in p['symbol']:
        ex_link_qty = abs(p['contracts'])
        print(f"Exchange {p['symbol']}: Qty={ex_link_qty}")

print(f"Total System Qty: {system_link_qty:.3f}")
print(f"Total Exch Qty  : {ex_link_qty:.3f}")
print(f"Diff            : {system_link_qty - ex_link_qty:.3f} LINK")

conn.close()
