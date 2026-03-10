
import logging
import sys
import os
import time

# Add current dir to path
sys.path.append(os.getcwd())

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

def force_flatten():
    print(f"=== FORCE FLATTEN V2 (Mode: {'TESTNET' if config.TESTNET else 'MAINNET'}) ===")
    ex = ExchangeInterface('future')
    
    # 1. Fetch positions
    positions = ex.fetch_positions()
    if positions is None:
        print("❌ Cannot fetch positions. Check API keys.")
        return

    active = [p for p in positions if abs(p['contracts']) > 0]
    if not active:
        print("✅ No active positions found.")
    else:
        print(f"Found {len(active)} positions. Attempting market close...")
        for p in active:
            symbol = p['symbol']
            contracts = abs(p['contracts'])
            side = 'sell' if p['contracts'] > 0 else 'buy'
            
            print(f"Closing {symbol} | Qty: {contracts} | Side: {side.upper()}...")
            
            try:
                # Try MARKET first
                res = ex.create_order(symbol, 'MARKET', side, contracts)
                if res:
                    print(f"  ✅ MARKET close successful for {symbol}")
                else:
                    print(f"  ⚠️ MARKET close returned empty for {symbol}")
            except Exception as e:
                print(f"  ❌ MARKET close failed for {symbol}: {e}")
                
                # Fallback: Try LIMIT at market price
                print(f"  🔄 Attempting LIMIT fallback for {symbol}...")
                try:
                    price = ex.get_last_price(symbol)
                    if price:
                        # Slip it slightly to ensure fill
                        adj_price = price * 1.02 if side == 'buy' else price * 0.98
                        res = ex.create_order(symbol, 'LIMIT', side, contracts, price=adj_price)
                        print(f"  ✅ LIMIT fallback successful for {symbol} @ {adj_price}")
                except Exception as e2:
                    print(f"  ❌ LIMIT fallback also failed for {symbol}: {e2}")
            
            time.sleep(1)

    # 2. Cancel all orders just in case
    print("\n[2] Cancelling all open orders...")
    try:
        # Get all distinct symbols from positions and common ones
        all_syms = list({p['symbol'] for p in positions}) + ['SOL/USDC:USDC', 'XRP/USDC:USDC', 'BTC/USDC:USDC', 'ETH/USDC:USDC']
        for s in set(all_syms):
            try:
                ex.cancel_all_orders(s)
                print(f"  ✅ Cancelled all for {s}")
            except: pass
    except Exception as e:
        print(f"  ❌ Order cancellation failed: {e}")

    print("\n=== Audit After Flatten ===")
    after = ex.fetch_positions()
    active_after = [p for p in (after or []) if abs(p['contracts']) > 0]
    if not active_after:
        print("✨ SUCCESS: Exchange is now FLAT.")
    else:
        print(f"⚠️ STILL ACTIVE: {active_after}")

if __name__ == "__main__":
    force_flatten()
