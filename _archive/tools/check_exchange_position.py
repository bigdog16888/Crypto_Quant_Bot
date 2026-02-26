import sys
import os
import logging

# Add project root to path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from config.settings import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PositionChecker")

def check_positions():
    print("--- CHECKING EXCHANGE POSITIONS ---")
    try:
        # Check both Spot and Future if applicable, or just Future
        market_types = ['future'] # User creates crypto bot mostly for futures
        
        for mt in market_types:
            print(f"\nChecking {mt.upper()} Market...")
            ex = ExchangeInterface(market_type=mt)
            positions = ex.fetch_positions()
            
            if not positions:
                print(f"✅ No positions found on {mt}.")
            else:
                print(f"⚠️ FOUND {len(positions)} POSITIONS on {mt}:")
                for p in positions:
                    print(f"   - {p['symbol']}: {p['side']} {p['contracts']} contracts (Entry: {p['entryPrice']}, uPnL: {p['unrealizedPnl']})")

    except Exception as e:
        print(f"❌ Error checking positions: {e}")

if __name__ == "__main__":
    check_positions()
