import sys
import os
import sqlite3
import json
import time

# Add project root
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.exchange_interface import ExchangeInterface
from config.settings import config

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def verify_round_10():
    print("="*60)
    print("ROUND 10: ENTRY & FLAT CHECK")
    print("="*60)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Check Bot 37 Specifics
    cursor.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE id=37")
    b37 = cursor.fetchone()
    print(f"\n[Bot 37] {b37[1]} ({b37[2]})")
    print(f" - Direction: {b37[3]}")
    print(f" - Active: {b37[4]}")
    
    # 1b. Check Current Price
    try:
        from engine.exchange_interface import ExchangeInterface
        exchange = ExchangeInterface(market_type='future')
        ticker = exchange.fetch_ticker(b37[2]) # pair from DB
        current_price = ticker['last']
        print(f" - Current Price: {current_price}")
    except Exception as e:
        print(f" - Error fetching price: {e}")
    
    # 2. Check Trade Status
    cursor.execute("SELECT total_invested, current_step FROM trades WHERE bot_id=37")
    t37 = cursor.fetchone()
    print(f" - Trade Status (DB): Invested ${t37[0]:.2f} (Step {t37[1]})")
    
    if t37[0] > 0:
        print("   ✅ Bot 37 has ENTERED a trade in the DB.")
    else:
        print("   ⚠️ Bot 37 has NOT entered a trade in the DB.")

    # 3. Check Open Orders in DB for Bot 37
    cursor.execute("SELECT count(*) FROM bot_orders WHERE bot_id=37 AND status='open'")
    o37 = cursor.fetchone()[0]
    print(f" - Open Orders (DB): {o37}")
    
    # 4. Exchange Flat Check
    print("\n--- Exchange Position Check ---")
    try:
        exchange = ExchangeInterface(market_type='future')
        positions = exchange.fetch_positions()
        
        has_pos = False
        for p in positions:
            qty = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if qty != 0:
                print(f" ❌ FOUND POSITION: {p['symbol']} = {qty}")
                has_pos = True
        
        if not has_pos:
            print(" ✅ Exchange is FLAT (No Positions).")
        else:
            print(" ⚠️ Exchange is NOT FLAT.")
            
    except Exception as e:
        print(f"Error checking exchange: {e}")

    conn.close()

if __name__ == "__main__":
    verify_round_10()
