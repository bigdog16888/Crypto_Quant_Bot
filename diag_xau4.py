import sys, os
sys.path.append(os.path.abspath('.'))
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface()
pos = ex.fetch_positions()
for p in pos:
    if 'XAU' in p['symbol']:
        calc = abs(p['contracts']) * p['entryPrice']
        info = p.get('info', {})
        notional_api = abs(float(info.get('notionalValue', info.get('positionAmt', 0)) or 0))
        if notional_api == 0 or notional_api == abs(float(info.get('positionAmt', 0))):
             notional_api = abs(float(info.get('notional', 0)))
             if notional_api == 0:
                 # Last resort, manually calculate if standard keys are missing
                 notional_api = abs(float(info.get('positionAmt', 0))) * float(p['entryPrice'])
                 
        print(f"XAU Contracts: {p['contracts']}")
        print(f"Entry Price:   {p['entryPrice']}")
        print(f"Math Val:      {calc}")
        print(f"Notional API:  {notional_api}")
        print(f"Diff vs Notional: {calc - notional_api}")
        print("Raw Info Dump:", info)
