import os
import sys
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def nuke_all():
    ex = ExchangeInterface(market_type='future')
    print("🚀 NUCLEAR OPTION: CLOSING ALL POSITIONS AND ORDERS...")
    try:
        # 1. Cancel all orders for all symbols
        ex.exchange.cancel_all_orders()
        print("✅ All orders cancelled.")
        
        # 2. Fetch all positions and close them
        positions = ex.fetch_positions()
        for pos in positions:
            size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if size != 0:
                symbol = pos.get('symbol')
                side = 'sell' if size > 0 else 'buy'
                print(f"Closing position on {symbol}: {size} via {side}...")
                ex.exchange.create_order(symbol, 'market', side, abs(size))
                print(f"✅ {symbol} closed.")
        
        time.sleep(3) # Wait for exchange to settle
        
        # 3. Enable Hedge mode
        print("RE-ENABLING BINANCE HEDGE MODE...")
        ex.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
        print("✅ SUCCESS: Hedge Mode ENABLED.")
        
    except Exception as e:
        print(f"❌ FAILED: {e}")

if __name__ == "__main__":
    nuke_all()
