
import sys
import os
import json
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface

def test_fetch_positions():
    print("="*60)
    print("TESTING FETCH POSITIONS ARGUMENTS")
    print("="*60)
    
    try:
        ex = ExchangeInterface(market_type='future')
        pair = 'ETH/USDC' # Use a known active pair
        
        # 1. Fetch All
        print("\n1. fetch_positions() [No Args]")
        all_pos = ex.exchange.fetch_positions()
        print(f"   Returned {len(all_pos)} positions.")
        
        # 2. Fetch Specific
        print(f"\n2. fetch_positions(['{pair}']) [List Arg]")
        spec_pos = ex.exchange.fetch_positions([pair])
        print(f"   Returned {len(spec_pos)} positions.")
        
        # Compare
        print("\n[Comparison]")
        if len(spec_pos) == 0 and len(all_pos) > 0:
            print("   ⚠️  WARNING: Specific fetch returned 0 while global fetch found positions.")
            print("   This might mean filtering by symbol failed (mismatched formats?)")
            
            # Check if ETH/USDC exists in all_pos
            found_in_all = any(p['symbol'] == pair for p in all_pos)
            print(f"   Does {pair} exist in All Positions? {found_in_all}")
            
            # Print symbols found in all_pos
            print("   Symbols found:", [p['symbol'] for p in all_pos])
            
        elif len(spec_pos) > 0:
            print("   ✅ Specific fetch matched!")
            print("   Symbol returned:", spec_pos[0]['symbol'])
            
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    test_fetch_positions()
