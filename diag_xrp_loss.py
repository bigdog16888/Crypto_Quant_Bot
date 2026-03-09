import sys, os, json, time
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
c = conn.cursor()

print("=== XRP BOT STATE (DB) ===")
c.execute("SELECT * FROM trades WHERE bot_id=10017")
t = c.fetchone()
c.execute("PRAGMA table_info(trades)")
cols = [r[1] for r in c.fetchall()]
if t:
    for col, val in zip(cols, t):
        print(f"  {col}: {val}")

print("\n=== XRP RECENT BOT ORDERS ===")
c.execute("""SELECT client_order_id, order_type, order_id, price, amount, status, created_at
             FROM bot_orders WHERE bot_id=10017
             ORDER BY created_at DESC LIMIT 15""")
for r in c.fetchall():
    print(f"  {r[1]:6} {r[4] or 0:8.3f} @ {r[3] or 0:8.4f}  status={r[4]}  {r[0]}")

print("\n=== ALL OPEN ORDERS (DB) ===")
c.execute("SELECT b.name, bo.order_type, bo.order_id, bo.price, bo.amount, bo.status FROM bot_orders bo JOIN bots b ON b.id=bo.bot_id WHERE bo.status='open' ORDER BY b.name")
for r in c.fetchall():
    print(f"  {r[0]:<20} {r[1]:6} id={r[2]}  {r[4] or 0:.4f} @ {r[3] or 0:.4f}")

conn.close()

print("\n=== EXCHANGE XRP POSITION ===")
ex = ExchangeInterface()
positions = ex.fetch_positions()
for p in positions:
    if 'XRP' in p.get('symbol',''):
        qty = p.get('contracts', 0)
        entry = p.get('entryPrice', 0)
        notional = abs(qty) * entry
        print(f"  {p['symbol']} qty={qty} entry={entry} notional=${notional:.2f} side={p.get('side')}")

print("\n=== EXCHANGE OPEN ORDERS (XRP) ===")
orders = ex.fetch_open_orders('XRP/USDC:USDC')
for o in orders:
    print(f"  {o.get('side'):5} {o.get('type'):6} {o.get('amount') or 0:.3f} @ {o.get('price') or 0:.4f} id={o.get('id')} cid={o.get('clientOrderId','')}")
