"""Check ALL XAU orders for CQB tags"""
from engine.exchange_interface import ExchangeInterface
import time

ex = ExchangeInterface(market_type='future')

# Fetch ALL closed orders for XAU
print("Fetching ALL XAU orders from exchange...")
since = int((time.time() - 30 * 24 * 3600) * 1000)  # Last 30 days

try:
    orders = ex.exchange.fetch_closed_orders('XAU/USDT:USDT', since=since, limit=100)
    
    cqb_orders = [o for o in orders if 'CQB_' in (o.get('clientOrderId') or '')]
    non_cqb_orders = [o for o in orders if 'CQB_' not in (o.get('clientOrderId') or '')]
    
    print(f"\nTOTAL XAU ORDERS: {len(orders)}")
    print(f"  CQB_ tagged (BOT): {len(cqb_orders)}")
    print(f"  Non-CQB (manual/other): {len(non_cqb_orders)}")
    
    if cqb_orders:
        print("\nCQB-TAGGED ORDERS:")
        for o in cqb_orders:
            print(f"  {o.get('datetime')} | {o.get('side')} | {o.get('clientOrderId')}")
    else:
        print("\n*** NO CQB-TAGGED XAU ORDERS FOUND ***")
        print("This means the XAU position was NOT placed by your bot system!")
        
    print("\nRECENT NON-CQB ORDERS (possible source of position):")
    for o in non_cqb_orders[-10:]:
        print(f"  {o.get('datetime')} | {o.get('side'):5} | {o.get('amount')} @ ${o.get('price')} | {o.get('clientOrderId', 'no-id')[:40]}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
