from engine.exchange_interface import ExchangeInterface
import time

ex = ExchangeInterface()
symbol = 'ETH/USDC:USDC'

print(f"--- SURGICAL FLATTEN: {symbol} ---")
try:
    pos = ex.fetch_positions()
    target = None
    for p in pos:
        if p['symbol'] == symbol:
            target = p
            break
    
    if target and abs(target['contracts']) > 0:
        side = 'sell' if target['contracts'] > 0 else 'buy'
        amount = abs(target['contracts'])
        print(f"Closing {amount} {side} on {symbol}...")
        order = ex.create_order(symbol, 'market', side, amount)
        print(f"Success: {order}")
    else:
        print("No active position found for this symbol.")
except Exception as e:
    print(f"Error: {e}")
