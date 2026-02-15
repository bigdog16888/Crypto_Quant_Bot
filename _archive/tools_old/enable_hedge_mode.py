import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def enable_hedge_mode():
    ex = ExchangeInterface(market_type='future')
    print("ENABLING BINANCE HEDGE MODE...")
    try:
        # Check current mode first
        mode = ex.exchange.fapiPrivateGetPositionSideDual()
        if mode.get('dualSidePosition'):
            print("✅ Hedge Mode is already ENABLED.")
            return
        
        # Change to Dual Side
        ex.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
        print("✅ SUCCESS: Hedge Mode ENABLED.")
    except Exception as e:
        print(f"❌ FAILED to enable hedge mode: {e}")
        print("Note: If you have open positions, you CANNOT change mode. Close them first.")

if __name__ == "__main__":
    enable_hedge_mode()
