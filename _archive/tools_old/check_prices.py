
import sys
import os
import time

# Add root to path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface

def check_prices():
    print("Initializing Exchange...")
    # Helper to print price
    def show(ex, symbol):
        try:
            ticker = ex.fetch_ticker(symbol)
            print(f"{symbol} Price: {ticker['last']}")
            return ticker['last']
        except Exception as e:
            print(f"Error {symbol}: {e}")
            return None

    ex_spot = ExchangeInterface(market_type='spot', validate=False)
    ex_future = ExchangeInterface(market_type='future', validate=False)
    
    print("\n--- Spot Prices ---")
    show(ex_spot, "BTC/USDT")
    show(ex_spot, "XAU/USDT") # Unlikely to exist on Spot?
    
    print("\n--- Futures Prices ---")
    show(ex_future, "BTC/USDT")
    xau_p = show(ex_future, "XAU/USDT")
    
    if xau_p:
        print(f"XAU Price is {xau_p}. If bot sees 5560, and this is {xau_p}, we have a mismatch.")

if __name__ == "__main__":
    check_prices()
