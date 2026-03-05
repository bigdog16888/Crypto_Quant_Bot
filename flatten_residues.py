import os
import json
from dotenv import load_dotenv
import ccxt

load_dotenv()

def main():
    ex = ccxt.binanceusdm({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_API_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    ex.set_sandbox_mode(True)
    
    # Needs to be rounded to precision. Let's just hardcode safe close quantities based on the user's previous output.
    # Previous output: SOLUSDC SHORT: qty=-2.5000.  If virtual is $104, that's ~1.15 SOL. 2.50 - 1.15 = 1.35 SOL to buy.
    # Previous output: ETHUSDC SHORT: qty=-0.2070. If virtual is $0, that's ~0 ETH. 0.207 ETH to buy.
    
    sol_reduce = 1.35
    eth_reduce = 0.207
    
    try:
        print(f"Executing Market BUY to reduce SOL short by {sol_reduce}...")
        order_sol = ex.create_order(
            symbol='SOL/USDC',
            type='market',
            side='buy',
            amount=sol_reduce,
            params={'reduceOnly': True}
        )
        print("SOL Success:", order_sol.get('id'))
    except Exception as e:
        print("SOL Error:", e)
        
    try:
        print(f"Executing Market BUY to reduce ETH short by {eth_reduce}...")
        order_eth = ex.create_order(
            symbol='ETH/USDC',
            type='market',
            side='buy',
            amount=eth_reduce,
            params={'reduceOnly': True}
        )
        print("ETH Success:", order_eth.get('id'))
    except Exception as e:
        print("ETH Error:", e)

if __name__ == '__main__':
    main()
