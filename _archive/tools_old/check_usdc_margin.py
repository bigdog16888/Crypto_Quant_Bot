
import sys
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
import json

ex = ExchangeInterface(market_type='future')
bal = ex.exchange.fetch_balance()
usdc = bal.get('USDC', {})
print(f"USDC Balance: {json.dumps(usdc, indent=2)}")

orders = ex.exchange.fetch_open_orders()
total_order_margin = 0
for o in orders:
    if o.get('symbol') and 'USDC' in o.get('symbol'):
        # For simplicity, assume 20x leverage
        total_order_margin += float(o.get('amount', 0)) * float(o.get('price', 0)) / 20.0
        
print(f"Estimated Margin Locked in Orders: {total_order_margin}")
