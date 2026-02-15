#!/usr/bin/env python3
"""Clean up all stale orders on exchange (one-time reset)."""

import sys
sys.path.insert(0, '.')

from config.settings import config
from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 60)
    print("STALE ORDER CLEANUP")
    print("=" * 60)
    
    # Only cancel orders WITHOUT CQB_ tag (legacy orders)
    # This preserves current bot orders while cleaning up old ones
    
    ex = ExchangeInterface(market_type='future')
    
    # Fetch all open orders
    print("\n📋 Fetching open orders...")
    # Use wrapper
    orders = ex.fetch_open_orders()
    print(f"   Found {len(orders)} open orders")
    
    # Find orders without CQB tag
    stale_orders = []
    bot_orders = []
    
    for o in orders:
        client_id = o.get('clientOrderId', '')
        if client_id.startswith('CQB_'):
            bot_orders.append(o)
        else:
            stale_orders.append(o)
    
    print(f"   Bot orders (CQB_): {len(bot_orders)} - KEEPING")
    print(f"   Legacy orders: {len(stale_orders)} - TO CANCEL")
    
    if not stale_orders:
        print("\n✅ No stale orders to clean up!")
        return
    
    # Confirm before cancelling
    print("\n🗑️ Cancelling stale orders...")
    cancelled = 0
    failed = 0
    
    for o in stale_orders:
        try:
            # Use wrapper which handles 'order not found' gracefully
            ex.cancel_order(o['id'], o['symbol'])
            print(f"   ✅ Cancelled: {o['symbol']} {o['side']} @ {o.get('price')}")
            cancelled += 1
        except Exception as e:
            print(f"   ❌ Failed: {o['id']}: {e}")
            failed += 1
    
    print(f"\n✅ Cleanup complete: {cancelled} cancelled, {failed} failed")
    print("=" * 60)

if __name__ == "__main__":
    main()
