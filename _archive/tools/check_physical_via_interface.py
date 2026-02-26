
import sys
import os
import json

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from engine.exchange_interface import ExchangeInterface
    # We don't need to import config here manually, ExchangeInterface does it.
    
    def check():
        print("🔍 Checking Position via Engine Interface (Sync)...")
        
        # Initialize Interface
        try:
            exchange = ExchangeInterface(market_type='future')
            print("✅ Interface Initialized.")
        except Exception as e:
            print(f"❌ Init Failed: {e}")
            return

        try:
            # Fetch Position
            positions = exchange.fetch_positions()
            
            # Filter for BTC/USDC
            btc_pos = None
            if positions:
                for p in positions:
                    if 'BTC' in p['symbol'] and 'USDC' in p['symbol']:
                        btc_pos = p
                        break
            
            if btc_pos:
                print(f"✅ POSITION FOUND: {btc_pos['contracts']} {btc_pos['side']} @ {btc_pos['entryPrice']}")
                print(f"   Raw: {btc_pos}")
            else:
                print("❌ NO POSITION FOUND (Size=0)")
                # Print all found only if debug needed, but keeping it clean
                if positions:
                    print(f"   (Found {len(positions)} other positions: {[p['symbol'] for p in positions]})")
                else:
                    print("   (Position list is empty or None)")
                
        except Exception as e:
            print(f"⚠️ Error: {e}")

    if __name__ == "__main__":
        check()

except ImportError as e:
    print(f"❌ Import Error: {e}")
    print(f"   Sys Path: {sys.path}")
