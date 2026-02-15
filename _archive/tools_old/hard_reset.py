import ccxt
import sqlite3
import json
import time
import os
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import config
from engine.exchange_interface import ExchangeInterface

def hard_reset():
    print("🚨 STARTING HARD RESET PROTOCOL 🚨")
    print("===================================")
    
    # 1. Initialize Exchange
    print("\n1. Connecting to Exchange...")
    try:
        exchange = ExchangeInterface(market_type='future')
        print("   ✅ Connected to Binance Futures")
    except Exception as e:
        print(f"   ❌ Failed to connect: {e}")
        return

    # 2. Cancel All Orders
    print("\n2. Cancelling ALL Open Orders...")
    try:
        # Fetch open orders first to see what we're cancelling
        orders = exchange.fetch_open_orders()
        print(f"   Found {len(orders)} open orders.")
        
        if orders:
            # Extract unique symbols
            symbols_with_orders = set(o['symbol'] for o in orders)
            print(f"   Cancelling orders on {len(symbols_with_orders)} symbols: {symbols_with_orders}")
            
            for symbol in symbols_with_orders:
                try:
                    exchange.cancel_all_orders(symbol)
                    print(f"   ✅ Cancelled all on {symbol}")
                except Exception as e:
                    print(f"   ❌ Failed to cancel on {symbol}: {e}")
        else:
            print("   ✅ No open orders to cancel.")
            
    except Exception as e:
        print(f"   ⚠️ Error cancelling orders: {e}")

    # 3. Close All Positions
    print("\n3. Closing ALL Positions...")
    try:
        positions = exchange.fetch_positions()
        active_positions = [p for p in positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        
        if not active_positions:
            print("   ✅ No active positions found.")
        else:
            for pos in active_positions:
                symbol = pos['symbol']
                size = float(pos.get('contracts', 0) or pos.get('size', 0))
                side = pos['side'] # long or short
                
                print(f"   📉 Closing {symbol} ({side} {size})...")
                
                # Close via Market Order
                close_side = 'sell' if side == 'long' else 'buy'
                
                # Special params for futures
                params = {'reduceOnly': True}
                
                try:
                    exchange.create_order(symbol, 'market', close_side, size, params=params)
                    print(f"      ✅ Closed {symbol}")
                except Exception as close_err:
                    print(f"      ❌ Failed to close {symbol}: {close_err}")
                    
    except Exception as e:
        print(f"   ❌ Error fetching/closing positions: {e}")

    # 4. Reset Database
    print("\n4. Wiping Database State...")
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'crypto_bot.db')
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Reset Trades
        cursor.execute("UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, entry_confirmed=0, basket_start_time=0")
        print(f"   ✅ Reset {cursor.rowcount} trade records.")
        
        # Clear Orders
        cursor.execute("DELETE FROM bot_orders")
        print(f"   ✅ Deleted {cursor.rowcount} order records.")
        
        # Clear Ownership State
        cursor.execute("DELETE FROM bot_ownership_state")
        print(f"   ✅ Deleted {cursor.rowcount} ownership state records.")
        
        # Clear Ownership History
        cursor.execute("DELETE FROM bot_ownership_history")
        print(f"   ✅ Deleted {cursor.rowcount} ownership history records.")
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        print(f"   ❌ Database reset failed: {e}")

    print("\n===================================")
    print("✅ HARD RESET COMPLETE. SYSTEM IS CLEAN.")
    print("   You may now restart the bot safely.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == '--force':
        hard_reset()
    else:
        confirm = input("Are you sure you want to WIPE EVERYTHING? (type 'YES'): ")
        if confirm == 'YES':
            hard_reset()
        else:
            print("Cancelled.")
