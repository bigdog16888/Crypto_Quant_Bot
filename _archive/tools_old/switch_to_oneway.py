import os
import sys
import time
import ccxt

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

def switch_to_oneway():
    ex = ExchangeInterface(market_type='future')
    print("🚀 SWITCHING TO ONE-WAY MODE...")
    
    try:
        # 1. Cancel all orders for all symbols
        print("Cancelling all open orders...")
        active_symbols = set()
        # Find symbols from open orders first
        try:
            # We can't fetch all open orders without symbol on some accounts, 
            # so we'll fetch positions to find active pairs.
            positions = ex.fetch_positions()
            for p in positions:
                if float(p.get('contracts', 0) or p.get('size', 0) or 0) != 0:
                    active_symbols.add(p['symbol'])
        except: pass
        
        # Add common ones just in case
        for s in ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'BTC/USDC:USDC', 'ETH/USDC:USDC', 'XAU/USDT:USDT']:
            active_symbols.add(s)
            
        for symbol in active_symbols:
            try:
                ex.exchange.cancel_all_orders(symbol)
                print(f"✅ Orders cancelled for {symbol}")
            except: pass
        
        # 2. Fetch all positions and close them
        print("Closing all open positions...")
        positions = ex.fetch_positions()
        for pos in positions:
            size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if size != 0:
                symbol = pos.get('symbol')
                # In Hedge mode, we need to specify which side to close
                side_to_close = pos.get('side', '').upper()
                closing_side = 'sell' if side_to_close == 'LONG' else 'buy'
                
                print(f"Closing {side_to_close} position on {symbol}: {size} via {closing_side}...")
                params = {'positionSide': side_to_close}
                ex.exchange.create_order(symbol, 'market', closing_side, abs(size), params=params)
                print(f"✅ {symbol} {side_to_close} closed.")
        
        time.sleep(3) # Wait for exchange to settle
        
        # 3. Disable Hedge mode (Switch to One-Way)
        print("SWITCHING TO ONE-WAY MODE (dualSidePosition=false)...")
        try:
            ex.exchange.set_position_mode(False)
            print("✅ SUCCESS: One-Way Mode ENABLED.")
        except Exception as e:
            if "already in that mode" in str(e).lower():
                print("✅ One-Way Mode already active.")
            else:
                raise e
        
    except Exception as e:
        print(f"❌ FAILED: {e}")

if __name__ == "__main__":
    switch_to_oneway()
