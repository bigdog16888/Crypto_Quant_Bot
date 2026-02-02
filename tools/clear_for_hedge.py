import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def clear_btc_usdc():
    ex = ExchangeInterface(market_type='future')
    print("CLEARING BTC/USDC ORDERS FOR MODE SWITCH...")
    try:
        pair = 'BTC/USDC'
        ex.cancel_all_orders(pair)
        print(f"✅ All orders for {pair} cancelled.")
        
        # Now try to enable hedge mode again
        print("RE-ENABLING BINANCE HEDGE MODE...")
        ex.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
        print("✅ SUCCESS: Hedge Mode ENABLED.")
    except Exception as e:
        print(f"❌ FAILED: {e}")

if __name__ == "__main__":
    clear_btc_usdc()
