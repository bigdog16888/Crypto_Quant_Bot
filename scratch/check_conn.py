import sys
import os
sys.path.append(os.getcwd())

from config.settings import config as global_config
from engine.exchange_interface import ExchangeInterface

def check_conn():
    print(f"Market Type: {global_config.MARKET_TYPE}")
    ex = ExchangeInterface(market_type=global_config.MARKET_TYPE)
    print(f"Exchange ID: {ex.exchange.id}")
    try:
        bal = ex.exchange.fetch_balance()
        print("Connected successfully! Balance keys:", list(bal.keys())[:5])
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == '__main__':
    check_conn()
