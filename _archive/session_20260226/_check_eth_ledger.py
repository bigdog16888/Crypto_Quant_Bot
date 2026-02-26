from engine.exchange_interface import ExchangeInterface
import pandas as pd
import time

ex = ExchangeInterface()
symbol = 'ETH/USDC:USDC'
now = int(time.time() * 1000)
sixteen_thirteen = 1771834394000 # 16:13 in MS

print(f"=== ETH/USDC TRADES SINCE 16:13 ({sixteen_thirteen}) ===")
try:
    trades = ex.fetch_my_trades(symbol, since=sixteen_thirteen)
    if trades:
        for t in trades:
            dt = pd.to_datetime(t['timestamp'], unit='ms')
            print(f"{dt} | {t['side']:<4} | Amt: {t['amount']:<6} | Prc: {t['price']:<8} | Cost: {t['cost']:<8}")
        
        amounts = [t['amount'] if t['side']=='buy' else -t['amount'] for t in trades]
        print(f"\nNet Qty: {sum(amounts)}")
    else:
        print("No trades found since 16:13.")
except Exception as e:
    print(f"Error: {e}")
