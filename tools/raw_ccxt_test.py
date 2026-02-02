import os
import sys
import ccxt

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import config

def raw_test():
    exchange = ccxt.binance({
        'apiKey': config.API_KEY,
        'secret': config.API_SECRET,
        'options': {'defaultType': 'future'}
    })
    if config.TESTNET:
        exchange.set_sandbox_mode(True)
    
    symbol = 'BTC/USDC:USDC'
    side = 'buy'
    type = 'limit'
    amount = 0.002
    price = 76000 # Way below market
    params = {'positionSide': 'LONG'}
    
    print(f"DEBUG: Attempting raw CCXT create_order for {symbol}")
    print(f"Params: {params}")
    
    try:
        order = exchange.create_order(symbol, type, side, amount, price, params)
        print(f"✅ SUCCESS: Order {order['id']} placed.")
    except Exception as e:
        print(f"❌ FAILED: {e}")

if __name__ == "__main__":
    raw_test()
