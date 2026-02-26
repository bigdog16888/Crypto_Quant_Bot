
import sys
import os
import time
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface

def close_all_positions():
    print("--- CLOSING ALL POSITIONS ---")
    try:
        ex = ExchangeInterface(market_type='future')
        positions = ex.fetch_positions()
        
        if not positions:
            print("✅ No positions to close.")
            return

        print(f"⚠️ Found {len(positions)} active positions.")
        for p in positions:
            symbol = p['symbol']
            qty = abs(float(p['contracts']))
            side = p['side'].upper() # 'LONG' or 'SHORT'
            
            if qty == 0: continue
            
            print(f"  Closing {symbol} {side} {qty}...")
            
            # Close Logic: OPPOSITE side
            close_side = 'SELL' if side == 'LONG' else 'BUY'
            
            # Place Market Order
            try:
                ex.create_order(symbol, 'MARKET', close_side, qty)
                print(f"    ✅ Closed {symbol}.")
            except Exception as e:
                print(f"    ❌ Failed to close {symbol}: {e}")
                
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    close_all_positions()
