#!/usr/bin/env python3
"""Test direct order placement to debug insufficient balance error."""

import sys
sys.path.insert(0, '.')

from config.settings import config
from engine.exchange_interface import ExchangeInterface

def main():
    print("=" * 60)
    print("DIRECT ORDER TEST")
    print("=" * 60)
    
    # Initialize exchange
    ex = ExchangeInterface(market_type='future')
    
    # Test parameters (same as failing order)
    symbol = 'BTC/USDC'
    side = 'sell'
    amount = 0.002  # Minimum BTC amount
    price = 78717.0
    
    print(f"\n📋 Test Order: {side.upper()} {amount} {symbol} @ ${price}")
    
    # Check current position first
    print("\n📈 Current Position:")
    try:
        positions = ex.fetch_positions()
        for p in positions:
            if 'BTC' in p.get('symbol', '') and float(p.get('contracts', 0) or 0) != 0:
                print(f"   {p['symbol']}: {p.get('side')} {p.get('contracts')}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Try to place order
    print("\n🚀 Placing Test Order...")
    try:
        params = {'postOnly': True}
        result = ex.exchange.create_order(symbol, 'limit', side, amount, price, params)
        print(f"   ✅ SUCCESS! Order ID: {result.get('id')}")
        
        # Cancel immediately
        try:
            ex.exchange.cancel_order(result['id'], symbol)
            print(f"   🗑️ Test order cancelled.")
        except:
            pass
            
    except Exception as e:
        print(f"   ❌ FAILED: {e}")
        
        # Try with different params
        print("\n🔄 Trying without postOnly...")
        try:
            result = ex.exchange.create_order(symbol, 'limit', side, amount, price, {})
            print(f"   ✅ SUCCESS (no postOnly)! Order ID: {result.get('id')}")
            ex.exchange.cancel_order(result['id'], symbol)
            print(f"   🗑️ Test order cancelled.")
        except Exception as e2:
            print(f"   ❌ FAILED: {e2}")
            
            # Try with reduceOnly
            print("\n🔄 Trying with reduceOnly...")
            try:
                result = ex.exchange.create_order(symbol, 'limit', side, amount, price, {'reduceOnly': True})
                print(f"   ✅ SUCCESS (reduceOnly)! Order ID: {result.get('id')}")
                ex.exchange.cancel_order(result['id'], symbol)
                print(f"   🗑️ Test order cancelled.")
            except Exception as e3:
                print(f"   ❌ FAILED: {e3}")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
