import sys
import os
import json

sys.path.append(r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
trades = ex.fetch_my_trades('LINK/USDC', since=1773033500000, limit=50) # Approx 13:20 today
print(f"Found {len(trades)} trades")
for t in trades:
    print(t.get('time', t.get('timestamp', '')), t.get('side'), t.get('qty', t.get('amount')), t.get('price'), t.get('id', t.get('orderId')))
