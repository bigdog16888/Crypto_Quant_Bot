import sys
import os

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface

def cleanup_orders():
    print("🧹 CLEANING UP ORDERS (KEEPING POSITIONS) 🧹")
    print("===========================================")
    
    try:
        exchange = ExchangeInterface(market_type='future')
        
        # Cancel all on active pairs
        pairs = ['BTC/USDC:USDC', 'XAU/USDT:USDT']
        
        for pair in pairs:
            print(f"   Cancelling orders on {pair}...")
            try:
                exchange.cancel_all_orders(pair)
                print(f"   ✅ Cancelled {pair}")
            except Exception as e:
                print(f"   ⚠️ Error cancelling {pair}: {e}")
                
    except Exception as e:
        print(f"❌ Exchange Error: {e}")

    print("===========================================")

if __name__ == "__main__":
    cleanup_orders()
