import sys
import os

# Ensure project root is in path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
# from engine.utils import set_environment_variables 

# set_environment_variables()

def check_raw():
    print("--- RAW EXCHANGE CHECK (via ExchangeInterface) ---")
    try:
        # Initialize
        exchange = ExchangeInterface(market_type='future')
        
        print("Fetching Positions...")
        positions = exchange.fetch_positions()
        
        print(f"Found {len(positions)} active positions:")
        for p in positions:
            print(f"Symbol: {p['symbol']}")
            print(f"  > Side: {p['side']}")
            print(f"  > Contracts: {p['contracts']}")
            print(f"  > Entry: {p['entryPrice']}")
            print("-" * 40)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_raw()
