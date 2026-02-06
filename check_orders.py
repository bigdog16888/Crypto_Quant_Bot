"""Check what happened to the 6 orders"""
from engine.exchange_interface import ExchangeInterface
import time

ex = ExchangeInterface(market_type='future')

# Check BTC orders specifically
print("Checking BTC/USDC orders...")
try:
    btc_orders = ex.fetch_open_orders('BTC/USDC:USDC')
    print(f"BTC/USDC:USDC open orders: {len(btc_orders) if btc_orders else 0}")
    for o in btc_orders or []:
        print(f"  {o.get('id')}: {o.get('side')} {o.get('amount')} @ ${o.get('price')} | {o.get('clientOrderId')}")
except Exception as e:
    print(f"Error: {e}")

# Check ALL orders
print("\nChecking ALL open orders...")
try:
    all_orders = ex.fetch_open_orders()
    print(f"All open orders: {len(all_orders) if all_orders else 0}")
    for o in all_orders or []:
        print(f"  {o.get('symbol')}: {o.get('side')} {o.get('amount')} @ ${o.get('price')} | {o.get('clientOrderId')}")
except Exception as e:
    print(f"Error fetching all: {e}")

# Check recent closed orders (maybe they were hit)
print("\nChecking recent closed BTC orders...")
since = int((time.time() - 3600) * 1000)  # Last hour
try:
    closed = ex.exchange.fetch_closed_orders('BTC/USDC:USDC', since=since, limit=10)
    print(f"Recently closed: {len(closed)}")
    for o in closed:
        print(f"  {o.get('datetime')}: {o.get('status')} {o.get('side')} {o.get('amount')} @ ${o.get('price')}")
except Exception as e:
    print(f"Error: {e}")

# Current position
print("\nCurrent BTC position...")
positions = ex.fetch_positions()
for p in positions:
    if 'BTC' in p.get('symbol', ''):
        print(f"  {p.get('symbol')}: {p.get('side')} {p.get('contracts')} @ ${p.get('entryPrice')}")
