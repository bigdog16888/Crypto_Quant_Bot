
import os
import sys
# Force add current directory to path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def main():
    print("--- VERIFYING FIX WITH ENGINE CODE ---")
    try:
        interface = ExchangeInterface()
        print("1. Exchange Interface Initialized")
        
        # DEBUG: Check if markets are actually loaded
        if interface.exchange.markets:
            print(f"Markets Loaded: {len(interface.exchange.markets)}")
            try:
                sample_key = next(iter(interface.exchange.markets))
                print(f"Sample Market Key: {sample_key}")
                print(f"Sample Market ID: {interface.exchange.markets[sample_key]['id']}")
            except: pass
            
            # SPECIFIC DEBUG
            print(f"Type of markets_by_id: {type(interface.exchange.markets_by_id)}")
            if isinstance(interface.exchange.markets_by_id, dict):
                print(f"Has 'XAUUSDT' in markets_by_id? {'XAUUSDT' in interface.exchange.markets_by_id}")
                if 'XAUUSDT' in interface.exchange.markets_by_id:
                    val = interface.exchange.markets_by_id['XAUUSDT']
                    print(f"Value type for 'XAUUSDT': {type(val)}")
                    print(f"Value content: {str(val)[:200]}")
            else:
                print(f"markets_by_id is NOT a dict! Content sample: {str(interface.exchange.markets_by_id)[:100]}")
        else:
            print("WARNING: Markets NOT loaded in ExchangeInterface")

        print("\n--- FETCHING POSITIONS (Should use Unified Symbols) ---")
        positions = interface.fetch_positions()
        
        if not positions:
            print("No active positions found (or None returned).")
        else:
            for p in positions:
                print(f"FOUND: {p['symbol']} | Side: {p['side']} | Size: {p['contracts']} | Entry: {p['entryPrice']}")

    except Exception as e:
        print(f"CRITICAL ENGINE ERROR: {e}")

if __name__ == "__main__":
    main()
