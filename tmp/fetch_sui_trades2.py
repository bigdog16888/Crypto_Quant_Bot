import asyncio
import sys
import os
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface

def check_sui_trades():
    ex = ExchangeInterface('future')
    
    since = int((time.time() - 86400 * 2) * 1000)
    # Use the proven wrapper
    trades = ex.fetch_my_trades('SUIUSDC', since=since, limit=1000)
    
    print(f"Fetched {len(trades)} trades.")
    
    for t in trades:
        ts = t.get('timestamp', 0)
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000))
        side = t.get('side')
        qty = t.get('amount')
        cid = t.get('clientOrderId', '')
        oid = t.get('order', '')
        
        print(f"[{dt}] {side.upper()}: {qty} | OID: {oid}")

if __name__ == '__main__':
    check_sui_trades()
