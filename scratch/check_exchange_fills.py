import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface

def check_fills():
    exchange = ExchangeInterface()
    print("Fetching my trades (fills) from exchange for SOLUSDC...")
    try:
        trades = exchange.exchange.fetch_my_trades('SOL/USDC:USDC', limit=50)
        print(f"Fetched {len(trades)} trades:")
        for t in trades:
            print(f"Time: {t.get('datetime')}, Side: {t.get('side')}, Price: {t.get('price')}, Amount: {t.get('amount')}, OrderId: {t.get('order')}, Fee: {t.get('fee')}")
    except Exception as e:
        print(f"Error fetching trades: {e}")

if __name__ == '__main__':
    check_fills()
