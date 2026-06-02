import sys
import os
sys.path.append(os.getcwd())

from config.settings import config as global_config
from engine.exchange_interface import ExchangeInterface

def inspect_closed_orders():
    ex = ExchangeInterface(market_type=global_config.MARKET_TYPE)
    try:
        orders = ex.exchange.fetch_closed_orders('SUI/USDC:USDC', limit=10)
        print(f"Fetched {len(orders)} closed orders.")
        if orders:
            o = orders[0]
            print("Order Keys and Types:")
            for k, v in o.items():
                print(f"  {k}: {type(v)} = {v}")
    except Exception as e:
        print(f"Error fetching closed orders: {e}")

if __name__ == '__main__':
    inspect_closed_orders()
