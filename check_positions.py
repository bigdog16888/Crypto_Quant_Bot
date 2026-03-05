import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.exchange_interface import ExchangeInterface

ex = ExchangeInterface(market_type='future')
data = ex._raw_request('/fapi/v2/positionRisk')
if isinstance(data, list):
    active = [p for p in data if float(p.get('positionAmt', 0)) != 0]
    if active:
        print("Exchange positions:")
        for p in active:
            amt = float(p['positionAmt'])
            side = 'LONG' if amt > 0 else 'SHORT'
            notional = abs(float(p.get('notional', 0)))
            print(f"  {p['symbol']} {side}: qty={amt:.4f}, notional=${notional:.2f}")
    else:
        print("No open positions on exchange")
else:
    print("Unexpected response:", str(data)[:200])
