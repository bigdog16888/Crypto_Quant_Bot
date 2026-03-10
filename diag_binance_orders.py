import sys
import os
import json

sys.path.append(r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
history = ex.fetch_closed_orders('LINK/USDC', since=1773033500000, limit=20)
for o in history:
    print(f"Time: {o.get('timestamp')} | ID: {o.get('id')} | CID: {o.get('clientOrderId')} | Side: {o.get('side')} | Status: {o.get('status')} | Filled: {o.get('filled', 0)} / {o.get('amount', 0)}")
