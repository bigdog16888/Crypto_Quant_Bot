import asyncio
import sys
import os
import time

# Ensure we can import the engine
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface

def check_sui_trades():
    ex = ExchangeInterface('future')
    # Use the CCXT exchange object directly
    
    since = int((time.time() - 86400 * 2) * 1000)
    # The CCXT fetch_my_trades uses the standard unified format
    trades = ex.exchange.fetch_my_trades('SUI/USDC:USDC', since=since, limit=1000)
    
    print(f"Fetched {len(trades)} trades.")
    
    for t in trades:
        ts = t.get('timestamp', 0)
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000))
        side = t.get('side')
        qty = t.get('amount')
        # CCXT stores raw binance response in info
        info = t.get('info', {})
        cid = info.get('clientOrderId', '')
        oid = t.get('order', '')
        
        # Only print from yesterday morning to today
        print(f"[{dt}] {side.upper()}: {qty} | CID: {cid} | OID: {oid}")

if __name__ == '__main__':
    check_sui_trades()
