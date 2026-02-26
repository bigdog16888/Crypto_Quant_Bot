import sys
import os
import time
import logging

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection, get_all_bots
from engine.exchange_interface import ExchangeInterface
from config.settings import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ResetSystem")

def reset_system():
    print("⚠️  WARNING: This will cancel ALL futures orders and close ALL futures positions!")
    print("⚠️  It will also reset ALL bots in the database to 'Scanning' state.")
    print("⏳  Starting in 3 seconds...")
    time.sleep(3)
    
    # 1. Initialize Exchange
    try:
        exchange = ExchangeInterface(market_type='future')
        print("✅ Exchange Interface Initialized.")
    except Exception as e:
        print(f"❌ Failed to init exchange: {e}")
        return

    # 2. Cancel All Orders
    print("\n🗑️  Cancelling ALL Open Orders...")
    try:
        # Get all pairs from markets
        exchange._ensure_markets()
        if exchange.exchange.markets:
            for symbol in exchange.exchange.markets:
                if "/USDT" in symbol or "/USDC" in symbol:
                    try:
                        orders = exchange.fetch_open_orders(symbol)
                        if orders:
                            print(f"   Cancelling {len(orders)} orders on {symbol}...")
                            exchange.cancel_all_orders(symbol)
                    except Exception as e:
                        pass # Ignore errors on pairs with no orders
        print("✅ All Orders Cancelled (Best Effort).")
    except Exception as e:
        print(f"❌ Error cancelling orders: {e}")

    # 3. Close All Positions
    print("\n📉 Closing ALL Positions...")
    try:
        positions = exchange.fetch_positions()
        if positions:
            for pos in positions:
                symbol = pos['symbol']
                amt = abs(pos['contracts'])
                side = pos['side']
                
                if amt > 0:
                    print(f"   Closing {side} {amt} {symbol}...")
                    # Market Close
                    close_side = 'sell' if side.lower() == 'long' else 'buy'
                    exchange.create_order(symbol, 'market', close_side, amt, params={'reduceOnly': True})
        print("✅ All Positions Closed.")
    except Exception as e:
        print(f"❌ Error closing positions: {e}")

    # 4. Reset Database
    print("\n💾 Resetting Database State...")
    try:
        conn = get_connection()
        c = conn.cursor()
        
        # Reset Bots
        c.execute("UPDATE bots SET status='Scanning'")
        
        # Reset Trades
        c.execute("UPDATE trades SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=0")
        
        # Clear Orders
        c.execute("DELETE FROM bot_orders")
        c.execute("DELETE FROM active_positions")
        
        conn.commit()
        print("✅ Database Reset Complete (Bots=Scanning, Trades=0, Orders=Cleared).")
        
    except Exception as e:
        print(f"❌ Database Reset Failed: {e}")

    print("\n✨ SYSTEM IS CLEAN. You can start the Main Bot now.")

if __name__ == "__main__":
    reset_system()
