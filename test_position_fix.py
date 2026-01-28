
import sys
import os
sys.path.insert(0, '.')
from engine.exchange_interface import ExchangeInterface
from config.settings import config

def test_position_matching():
    print("="*60)
    print("TESTING POSITION SYMBOL MATCHING FIX")
    print("="*60)

    # 1. Fetch positions
    print("1. Fetching positions from Exchange...")
    try:
        ex = ExchangeInterface(market_type='future')
        positions = ex.exchange.fetch_positions()
        print(f"   Found {len(positions)} positions on exchange.")
    except Exception as e:
        print(f"   ERROR fetching positions: {e}")
        return

    # 2. Define pairs active in bots (from your dashboard)
    active_pairs = ['ETH/USDC', 'BNB/USDC', 'ADA/USDT', 'BTC/USDC', 'BTC/USDT']
    
    print("\n2. Testing Matching Logic:")
    
    for pair in active_pairs:
        print(f"\n   Checking Bot Pair: {pair}")
        
        # --- OLD LOGIC (FAILING) ---
        old_match = any(
            float(p.get('contracts', 0) or p.get('size', 0) or 0) != 0 
            for p in positions 
            if p and p.get('symbol') == pair
        )
        print(f"   [OLD LOGIC] Direct match '{pair}' == symbol? -> {old_match}")
        
        # --- NEW LOGIC (FIXED) ---
        new_match = False
        matched_symbol = None
        
        target_pair = pair.replace('/', '').split(':')[0]
        
        for p in positions:
            if not p: continue
            
            # The Logic I applied in bot_executor.py
            pos_symbol_raw = p.get('symbol', '')
            pos_symbol = pos_symbol_raw.replace('/', '').split(':')[0]
            
            if pos_symbol == target_pair:
                size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if size != 0:
                    new_match = True
                    matched_symbol = pos_symbol_raw
                    break
        
        print(f"   [NEW LOGIC] Normalized '{target_pair}' == '{matched_symbol}'? -> {new_match}")
        
        if new_match and not old_match:
            print("   ✅ SUCCESS: Fix correctly detects position that was previously missed!")
        elif new_match and old_match:
            print("   ℹ️  OK: Position was already detected (no change).")
        elif not new_match:
            print("   ⚠️  NOTE: No position found for this pair (might be correct if closed).")

if __name__ == "__main__":
    test_position_matching()
