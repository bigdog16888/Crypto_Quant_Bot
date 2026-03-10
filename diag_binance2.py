import sys
import os
import json

sys.path.append(r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
trades = ex.fetch_my_trades('LINK/USDC', since=1773033500000, limit=2) # Approx 13:20 today
for t in trades:
    print(json.dumps(t, indent=2))
