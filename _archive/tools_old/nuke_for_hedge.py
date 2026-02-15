import os
import sys
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def nuke_btc_usdc():
    ex = ExchangeInterface(market_type='future')
    print("☢️ NUKING BTC/USDC FOR MODE SWITCH...")
    try:
        pair = 'BTC/USDC'
        
        # 1. Cancel all orders
        ex.cancel_all_orders(pair)
        print("✅ Orders cancelled.")
        
        # 2. Fetch positions and close them
        # In One-Way Mode, we just need to see our net position
        positions = ex.fetch_positions()
        for pos in positions:
            if pos.get('symbol') == pair:
                size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                if size != 0:
                    side = 'sell' if size > 0 else 'buy'
                    print(f"Closing position of {size} via {side}...")
                    ex.exchange.create_order(pair, 'market', side, abs(size))
                    print("✅ Position closed.")
        
        time.sleep(2) # Wait for exchange to settle
        
        # 3. Enable Hedge mode
        print("RE-ENABLING BINANCE HEDGE MODE...")
        ex.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
        print("✅ SUCCESS: Hedge Mode ENABLED.")
        
    except Exception as e:
        print(f"❌ FAILED: {e}")

if __name__ == "__main__":
    nuke_btc_usdc()
