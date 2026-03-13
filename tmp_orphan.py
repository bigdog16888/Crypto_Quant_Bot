import time
import sys; sys.path.append('.')
from engine.exchange_interface import ExchangeInterface

try:
    ex = ExchangeInterface(market_type='future')
    
    # 1773374000000 is ~ 12:13 PM, just before the restart.
    hist = ex.fetch_my_trades('SUI/USDC:USDC', since=1773374000000, limit=1000)
    
    print(f"Fetched {len(hist)} trades since DB wipe...")
    
    unknown_sells = 0
    unknown_buys = 0
    
    for g in hist:
        amt = float(g.get('amount', 0))
        side = g.get('side', '')
        cid = g.get('info', {}).get('clientOrderId', '')
        ts = g.get('timestamp')
        
        # If it doesn't have a CQB client ID, it was an orphaned limit order from before the wipe
        if not cid.startswith('CQB_'):
            if side.lower() == 'sell': 
                unknown_sells += amt
            else: 
                unknown_buys += amt
            if amt > 1000:
                print(f"LARGE ORPHAN FILL: {side.upper()} {amt} @ {g.get('price')} CID: '{cid}' Time: {ts}")

    print(f"\nTotal Orphaned Fills (Non-CQB): SELL {unknown_sells}, BUY {unknown_buys}. Net Orphan = {unknown_buys - unknown_sells}")

except Exception as e:
    print('Error:', e)
