import sys, os
sys.path.append(os.path.abspath('.'))
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
pos = ex.fetch_positions()
for p in pos:
    if 'XAU' in p['symbol']:
        calc = abs(p['contracts']) * p['entryPrice']
        notional = float(p['info'].get('positionAmt', 0)) * float(p['info'].get('entryPrice', 0))
        notional_api = float(p['info'].get('notional', 0))
        print(f"XAU Contracts: {p['contracts']}")
        print(f"Entry Price:   {p['entryPrice']}")
        print(f"Math Val:      {calc}")
        print(f"Notional API:  {notional_api}")
        print(f"Math Notional: {notional}")
        print(f"Diff vs Notional: {calc - abs(notional_api)}")
