"""Check XAU order history on exchange"""
from engine.exchange_interface import ExchangeInterface
import time

ex = ExchangeInterface(market_type='future')

# Fetch recent closed orders for XAU
print("Fetching XAU order history from exchange...")
since = int((time.time() - 7 * 24 * 3600) * 1000)  # Last 7 days

try:
    orders = ex.exchange.fetch_closed_orders('XAU/USDT:USDT', since=since, limit=50)
    print(f"\nXAU CLOSED ORDERS (last 7 days): {len(orders)}")
    print("-" * 100)
    for o in orders[-20:]:  # Last 20
        cid = o.get('clientOrderId', '')
        is_bot = 'CQB_' in cid if cid else False
        print(f"  {o.get('datetime')} | {o.get('side'):4} | {o.get('amount')} @ ${o.get('price')} | Status: {o.get('status')} | {'BOT' if is_bot else 'MANUAL/UNKNOWN'} | {cid[:30] if cid else 'no-client-id'}")
except Exception as e:
    print(f"Error: {e}")

# Also check current position details
print("\n" + "=" * 80)
print("CURRENT XAU POSITION:")
positions = ex.fetch_positions()
for p in positions:
    if 'XAU' in p.get('symbol', ''):
        print(f"  Symbol: {p.get('symbol')}")
        print(f"  Side: {p.get('side')}")
        print(f"  Contracts: {p.get('contracts')}")
        print(f"  Entry Price: ${p.get('entryPrice')}")
        print(f"  Leverage: {p.get('leverage')}")
