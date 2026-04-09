import asyncio
import sys
import os
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface

def check_sui_trades():
    ex = ExchangeInterface('future')
    
    since = int((time.time() - 86400 * 2) * 1000)
    trades = ex.fetch_my_trades('SUIUSDC', since=since, limit=1000)
    
    print(f"Fetched {len(trades)} trades.")
    
    for t in trades:
        qty = t.get('amount')
        side = t.get('side')
        ts = t.get('timestamp', 0)
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000))
        # Look for 1052.7
        if float(qty) > 1000:
            print(f"[{dt}] {side.upper()}: {qty}")

if __name__ == '__main__':
    check_sui_trades()
