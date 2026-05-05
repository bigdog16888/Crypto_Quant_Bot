import os
import sys

# Add project root to sys.path
sys.path.append(r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')

from engine.exchange_interface import ExchangeInterface
import config

# Mock config for testnet/demo
config.TESTNET = True
config.DEMO_TRADING = True

ex = ExchangeInterface()
print("--- FETCHING POSITIONS ---")
pos = ex.fetch_positions()
if pos:
    for p in pos:
        print(p)
else:
    print("No positions found.")
