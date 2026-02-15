import os
import sys
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def nuke_specific():
    ex = ExchangeInterface(market_type='future')
    print("☢️ NUKING BTC/USDC AND GOLD...")
    try:
        symbols = ['BTC/USDC', 'BTC/USDT', 'XAU/USDT:USDT']
        
        for symbol in symbols:
            try:
                print(f"Cleaning {symbol}...")
                ex.exchange.cancel_all_orders(symbol)
            except: pass
            
        # Fetch all positions and close them
        positions = ex.fetch_positions()
        for pos in positions:
            size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if size != 0:
                s = pos.get('symbol')
                
                # Determine side and positionSide based on current position size
                if size > 0:
                    side = 'sell'
                    position_side = 'LONG'
                else:
                    side = 'buy'
                    position_side = 'SHORT'
                
                print(f"Closing {s}: {abs(size)} unit(s) via {side} (positionSide: {position_side})...")
                # HEDGE MODE: Must specify positionSide
                ex.exchange.create_market_order(s, side, abs(size), params={'positionSide': position_side})
                print(f"✅ {s} closed.")
        
        time.sleep(3)
        
        print("RE-ENABLING BINANCE HEDGE MODE...")
        ex.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
        print("✅ SUCCESS: Hedge Mode ENABLED.")
        
    except Exception as e:
        print(f"❌ FAILED: {e}")

if __name__ == "__main__":
    nuke_specific()
