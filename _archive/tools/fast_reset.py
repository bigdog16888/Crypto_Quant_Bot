import sys
import os
import time
import logging
import sqlite3

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FastReset")

def fast_reset():
    print("🚀 Starting FAST Reset...")
    
    # 1. Get Target Pairs from DB
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT DISTINCT pair FROM bots")
    db_pairs = [r[0] for r in c.fetchall()]
    print(f"📋 Found {len(db_pairs)} pairs in DB to check: {db_pairs}")
    
    # 2. Init Exchange
    try:
        exchange = ExchangeInterface(market_type='future')
    except Exception as e:
        print(f"❌ Failed to init exchange: {e}")
        return

    # 3. Get Open Positions to add to target list
    print("🔍 Fetching Open Positions...")
    try:
        positions = exchange.fetch_positions()
        if positions:
            for p in positions:
                sym = p['symbol']
                if sym not in db_pairs:
                    db_pairs.append(sym)
                    print(f"   + Added {sym} from active position.")
    except Exception as e:
        print(f"⚠️ Failed to fetch positions: {e}")

    # 4. Cancel & Close on Target Pairs
    print(f"⚡ Cleaning up {len(db_pairs)} pairs...")
    for pair in db_pairs:
        try:
            # Cancel Orders
            print(f"   Checking {pair}...")
            exchange.cancel_all_orders(pair)
            
            # Close Position
            pos = next((p for p in positions if p['symbol'] == normalize_symbol(pair)), None) if positions else None
            # Re-fetch specific position if needed? No, just use fetch_positions cache if possible or trust market Close
            # Actually, fetch_positions returns all.
            # If we missed it effectively, we can try to close blindly or skip.
            # Let's use the list we got.
            
            if pos and pos['contracts'] > 0:
                print(f"   CLOSING {pos['side']} {pos['contracts']} {pair}...")
                side = 'sell' if pos['side'].lower() == 'long' else 'buy'
                # User requested avoiding reduceOnly due to potential flip/conflict
                # Standard market order to close
                exchange.create_order(pair, 'market', side, pos['contracts'])
                
        except Exception as e:
            print(f"   ⚠️ Error on {pair}: {e}")

    # 5. Reset DB
    print("\n💾 Resetting Database...")
    try:
        c.execute("UPDATE bots SET status='Scanning'")
        c.execute("UPDATE trades SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=0")
        c.execute("DELETE FROM bot_orders")
        c.execute("DELETE FROM active_positions")
        conn.commit()
        print("✅ Database Reset Done.")
    except Exception as e:
        print(f"❌ DB Reset Failed: {e}")
        
    print("\n✨ FAST RESET COMPLETE.")

if __name__ == "__main__":
    fast_reset()
