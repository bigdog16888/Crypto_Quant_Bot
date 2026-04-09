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
    
    total_sell = 0.0
    for t in trades:
        qty = float(t.get('amount', 0))
        side = str(t.get('side', '')).lower()
        ts = t.get('timestamp', 0)
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts/1000))
        
        # Before the 18:13 buy
        if ts < 1774894415000: # approximation of 18:13 unix timestamp
            if side == 'sell':
                total_sell += qty
                
    print(f"Total SUI sold across all micro-trades before 18:13: {total_sell}")

if __name__ == '__main__':
    check_sui_trades()
