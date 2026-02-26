
import sys
import os
import time
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface

def create_ghost_order():
    print("--- CREATING GHOST ORDER ---")
    try:
        ex = ExchangeInterface(market_type='future')
        # Place an order for Bot 10002 (which is STOPPED)
        # Using a very far away price to avoid execution
        symbol = 'XAU/USDT'
        price = 1500.0 # Way below current price (~2600)
        qty = 0.01 # Min size
        
        # Client ID mimics a real bot order
        cid = f"CQB_10002_GHOST_TEST_{int(time.time())}"
        
        print(f"Placing LIMIT BUY for {symbol} @ {price} with CID={cid}...")
        res = ex.create_order(symbol, 'LIMIT', 'BUY', qty, price, params={'clientOrderId': cid})
        
        print(f"✅ Ghost Order Created: {res['id']}")
        return res['id']

    except Exception as e:
        print(f"❌ Failed to create ghost order: {e}")
        return None

if __name__ == "__main__":
    create_ghost_order()
