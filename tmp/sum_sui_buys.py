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
    
    total_buy = 0.0
    for t in trades:
        qty = float(t.get('amount', 0))
        side = str(t.get('side', '')).lower()
        ts = t.get('timestamp', 0)
        
        # between 07:40:00 (1774914000) and 09:30:33 (1774920633) TODAY
        if ts > 1774914000000 and ts < 1774920633000:
            if side == 'buy':
                total_buy += qty
                print(f"Adding buy of {qty}")
                
    print(f"Total SUI explicitly bought on Binance between 07:40 and 09:30: {total_buy}")

if __name__ == '__main__':
    check_sui_trades()
