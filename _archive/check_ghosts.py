"""Check ghost orders for tags"""
from engine.exchange_interface import ExchangeInterface
import json

ex = ExchangeInterface(market_type='future')
print("Fetching XAU/USDT orders...")
orders = ex.fetch_open_orders('XAU/USDT')

print(f'\nFound {len(orders)} orders for XAU/USDT:')
for o in orders:
    oid = o.get('clientOrderId', 'N/A')
    print(f"ID: {o['id']} | Amt: {o['amount']} | Price: {o['price']} | Tag: {oid}")
