
import logging
import json
from engine.exchange_interface import ExchangeInterface

logging.basicConfig(level=logging.INFO)
ex = ExchangeInterface(market_type='future')

pairs = ['BTC/USDC', 'ETH/USDC', 'BTC/USDT']

for p in pairs:
    print(f"\n=== HISTORY FOR {p} ===")
    history = ex.fetch_closed_orders(p, limit=50)
    if not history:
        print("  No history found.")
        continue
        
    for o in history:
        if o['status'] == 'filled':
            print(f"  ✅ FILL: {o['id']} | {o['clientOrderId']} | {o['side']} | {o['amount']} @ {o['price']} | {o['timestamp']}")
        else:
            print(f"  ❌ {o['status'].upper()}: {o['id']} | {o['clientOrderId']}")
