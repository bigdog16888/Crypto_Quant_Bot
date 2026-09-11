"""SOL fill-credit attribution — final verification (read-only).

Proves the -28.95 delta closure with raw evidence:
1. Fetch CQB_100324_ENTRY_26_10's exchange order (GET only) — is the fill real?
2. Show 100324's fills sum exactly to its trades.open_qty.
3. Show pair virtual net = exchange net = -6.03.
NO WRITES anywhere.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3

LIVE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crypto_bot.db")
ro = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
ro.row_factory = sqlite3.Row

row = ro.execute(
    "SELECT order_id, client_order_id, order_type, price, amount, filled_amount, status, cycle_id, step "
    "FROM bot_orders WHERE client_order_id = 'CQB_100324_ENTRY_26_10'"
).fetchone()
print("=== 1. The credited order (DB row) ===")
print(dict(row))

print()
print("=== 2. Live exchange verification of that order_id (GET /fapi/v1/order) ===")
from engine.exchange_interface import ExchangeInterface
ex = ExchangeInterface()
ex_order = ex.fetch_order(str(row["order_id"]), "SOL/USDC:USDC", params={"timeout": 15000})
print(f"  exchange status={ex_order.get('status')}  filled={ex_order.get('filled')}  avg_price={ex_order.get('average')}  side={ex_order.get('side')}")

print()
print("=== 3. 100324 fills sum vs trades.open_qty ===")
fills = ro.execute(
    "SELECT client_order_id, filled_amount, price FROM bot_orders "
    "WHERE bot_id = 100324 AND filled_amount > 0 AND status = 'filled' ORDER BY updated_at"
).fetchall()
total = 0.0
for f in fills:
    total += float(f["filled_amount"])
    print(f"  {str(f['client_order_id'])[:44]:<46} {f['filled_amount']:>8} @ {f['price']}")
t = ro.execute("SELECT open_qty, total_invested, avg_entry_price FROM trades WHERE bot_id = 100324").fetchone()
print(f"  SUM of filled_amount        = {round(total, 8)}")
print(f"  trades.open_qty            = {t['open_qty']}   total_invested={t['total_invested']:.2f} avg={t['avg_entry_price']:.6f}")

print()
print("=== 4. Pair net vs exchange ===")
p = ro.execute("SELECT open_qty, position_side FROM trades WHERE bot_id = 100001").fetchone()
virtual = -float(p["open_qty"]) + float(t["open_qty"])
print(f"  virtual = -100001({p['open_qty']} SHORT) + 100324({t['open_qty']} LONG) = {round(virtual, 8)}")
for pos in ex.fetch_positions():
    if "SOL" in pos.get("symbol", ""):
        print(f"  exchange net_qty           = {pos.get('net_qty')}  side={pos.get('side')}")
ro.close()
