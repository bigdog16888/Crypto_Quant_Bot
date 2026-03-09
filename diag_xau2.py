import sys, os
sys.path.append(os.path.abspath('.'))
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
pos = ex.fetch_positions()
for p in pos:
    if 'XAU' in p['symbol']:
        print(f"Full XAU Position Dump:")
        for k, v in p.items():
            print(f"  {k}: {v}")
