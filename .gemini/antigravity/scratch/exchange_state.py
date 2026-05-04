"""
Nuclear Reset Pre-Check: Fetch live exchange state before any DB operations.
This is READ-ONLY — no writes anywhere.
"""
import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

print("Connecting to exchange...")
ex = ExchangeInterface(market_type=config.MARKET_TYPE)

print()
print("=== EXCHANGE PHYSICAL POSITIONS ===")
positions = ex.fetch_positions()
active = []
for p in (positions or []):
    qty = float(p.get('contracts', 0) or 0)
    if abs(qty) > 0:
        sym = p.get('symbol', '')
        entry = float(p.get('entryPrice', 0) or 0)
        notional = abs(qty) * entry
        side = 'LONG' if qty > 0 else 'SHORT'
        active.append((sym, side, abs(qty), entry, notional))
        print(f"  {sym}: {side} qty={abs(qty)} entry={entry:.4f} notional={notional:.2f}")

if not active:
    print("  (no open positions)")

print()
print("=== EXCHANGE OPEN ORDERS ===")
try:
    orders = ex.fetch_open_orders()
    if orders:
        order_by_bot = {}
        for o in orders:
            cid = o.get('clientOrderId', '')
            bid = cid.split('_')[1] if cid.startswith('CQB_') else 'UNKNOWN'
            if bid not in order_by_bot:
                order_by_bot[bid] = []
            order_by_bot[bid].append(o)
        for bid, bords in sorted(order_by_bot.items()):
            print(f"  Bot {bid}: {len(bords)} order(s)")
            for o in bords:
                print(f"    {o.get('symbol')} {o.get('side')} {o.get('type')} qty={o.get('amount')} px={o.get('price')}")
    else:
        print("  (no open orders)")
except Exception as e:
    print(f"  Error fetching orders: {e}")

print()
print("=== SUMMARY ===")
print(f"Total open positions: {len(active)}")
for sym, side, qty, entry, notional in active:
    print(f"  {normalize_symbol(sym)} {side}: {qty} @ {entry:.4f} = ${notional:.2f}")
