import sys
from engine.exchange_interface import ExchangeInterface
from config.settings import config
import json

def check_balance():
    try:
        print(f"--- CONFIGURATION ---")
        print(f"TESTNET MODE: {config.TESTNET}")
        print(f"MARKET TYPE: {config.MARKET_TYPE}")
        
        print("\n--- CONNECTING TO EXCHANGE ---")
        ex = ExchangeInterface()
        
        print("\n--- FETCHING BALANCE ---")
        # specific to binance response structure
        balance = ex.fetch_balance()
        
        # print raw keys to see what Assets we have
        print(f"Assets found: {[k for k in balance.keys() if k not in ['info', 'timestamp', 'datetime', 'free', 'used', 'total']]}")
        
        print("\n--- DETAIL (USDT / USDC) ---")
        for asset in ['USDT', 'USDC']:
            if asset in balance:
                data = balance[asset]
                print(f"{asset}:")
                # Handle structure which might be {'free': ..., 'used': ..., 'total': ...}
                # or nested differently depending on implementation
                if isinstance(data, dict):
                    print(f"  Free: {data.get('free', 0)}")
                    print(f"  Used: {data.get('used', 0)}")
                    print(f"  Total: {data.get('total', 0)}")
                else:
                    print(f"  Value: {data}")
            else:
                print(f"{asset}: Not found in wallet")

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    check_balance()
