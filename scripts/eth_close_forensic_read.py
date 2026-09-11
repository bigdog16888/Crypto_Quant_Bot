# ETH orphan close — post-fill forensic read (read-only)
import sys, json, datetime
sys.path.insert(0, ".")
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
FILL_MS = 1788503407067  # clientOrderId decode

print("=== TRADE 108108218 (the close fill) — full ===")
trades = ex.fetch_my_trades("ETH/USDC:USDC", since=FILL_MS - 60000, limit=10)
for t in trades:
    if str(t.get("order")) == "1012525341":
        ts = t.get("timestamp")
        print("id:", t.get("id"), "order:", t.get("order"))
        print("clientOrderId:", t.get("clientOrderId"))
        print("timestamp:", ts, "->", datetime.datetime.fromtimestamp(ts / 1000) if ts else None)
        print("side:", t.get("side"), "amount:", t.get("amount"), "price:", t.get("price"))
        print("cost:", t.get("cost"), "commission:", t.get("commission"), "fee:", t.get("fee"))
        print("symbol:", t.get("symbol"))

print()
print("=== INCOME ETH/USDC (fetch_income) ===")
try:
    inc = ex.fetch_income(symbol="ETH/USDC:USDC")
    rpnl = 0.0; comm = 0.0; fund = 0.0
    for e in (inc if isinstance(inc, list) else inc.get("result", [])):
        it = e.get("info", {}).get("incomeType") or e.get("type")
        amt = float(e.get("amount") or 0)
        ts = e.get("timestamp")
        when = datetime.datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M:%S") if ts else "?"
        print(f"{when} {it} {amt:+.4f} {e.get('code')} trade={e.get('trade')}")
except Exception as e:
    print("fetch_income wrapper err:", e)
    # fallback raw
    try:
        raw = ex.exchange.fapiPrivateGetIncome({"symbol": "ETHUSDC", "limit": 40})
        rpnl = 0.0; comm = 0.0
        for e in raw:
            amt = float(e.get("income") or 0)
            ts = datetime.datetime.fromtimestamp(int(e.get("time", 0)) / 1000).strftime("%m-%d %H:%M:%S")
            print(f"{ts} {e.get('incomeType'):14s} {amt:+.6f} tradeId={e.get('tradeId')}")
            if e.get("incomeType") == "REALIZED_PNL":
                rpnl += amt
            if e.get("incomeType") == "COMMISSION":
                comm += amt
        print(f"SUM realized={rpnl:+.4f} commission={comm:+.6f}")
    except Exception as e2:
        print("raw income err:", e2)

print()
print("=== BALANCE (full structure) ===")
b = ex.fetch_balance()
for asset, d in b.items():
    if isinstance(d, dict):
        tot = d.get("total")
        if tot:
            print(asset, json.dumps({k: d.get(k) for k in d if d.get(k) not in (None, 0)}))
