"""ETH close fill — exact-number re-pull. READ-ONLY. Single source: exchange.

Answers two operator questions with zero ambiguity:
  A) the close fill's commission (fee), exact digits
  B) order 1012525341's exact filled quantity
  C) authoritative realized PnL via income (best effort)
  D) PnL arithmetic recomputed by tool from pulled values (no mental math)
"""
import sys, json, datetime
sys.path.insert(0, ".")
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
FILL_MS = 1788503408994  # 2026-09-04 14:30:08.994 local (earlier pull)

print("=== A) TRADE 108108218 (exchange record) ===")
trades = ex.fetch_my_trades("ETH/USDC:USDC", since=FILL_MS - 120000, limit=20)
trade = None
for t in trades:
    if str(t.get("id")) == "108108218":
        trade = t
        break
if trade is None:
    print("TRADE NOT FOUND — STOP")
    sys.exit(1)
for k in ("id", "order", "side", "amount", "price", "cost", "commission", "symbol", "timestamp"):
    print(f"{k:12s}: {trade.get(k)!r}")
ts = trade.get("timestamp")
print("fill time    :", datetime.datetime.fromtimestamp(ts / 1000))

print()
print("=== B) ORDER 1012525341 (exchange record) ===")
o = ex.fetch_order("1012525341", "ETH/USDC:USDC")
for k in ("id", "clientOrderId", "amount", "filled", "average", "status"):
    print(f"{k:14s}: {o.get(k)!r}")

qty = float(o.get("filled"))
price_exit = float(trade.get("price"))
commission = float(trade.get("commission"))

print()
print("=== C) INCOME (authoritative REALIZED_PNL, best effort) ===")
try:
    inc = ex.fetch_income(symbol="ETH/USDC:USDC")
    if isinstance(inc, str):
        inc = json.loads(inc)
    print(json.dumps(inc, indent=1, default=str)[:4000])
except Exception as e:
    print("fetch_income err:", type(e).__name__, e)

print()
print("=== D) ARITHMETIC (tool-computed from pulled values) ===")
entry = 2405.95
gross = (price_exit - entry) * qty
print(f"entry price     = {entry}")
print(f"exit price      = {price_exit}")
print(f"filled qty      = {qty}")
print(f"close commission= {commission}")
print(f"gross PnL       = ({price_exit} - {entry}) * {qty} = {gross:.6f}")
print(f"net of close fee= {gross:.6f} - {commission:.6f} = {gross - commission:.6f}")
wallet_now = 3843.82574406 + 5278.00354853  # USDC + USDT totals from fetch_balance
print(f"wallet delta    = {wallet_now:.5f} - 9034.27 (14:14 Circuit Check) = {wallet_now - 9034.27:.6f}")
