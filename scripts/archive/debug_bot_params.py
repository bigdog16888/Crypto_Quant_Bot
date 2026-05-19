import sys
import os
import sqlite3
import json

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def debug_bot(bot_id):
    print(f"Connecting to DB at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT id, name, is_active, config, strategy_type, direction FROM bots WHERE id = ?', (bot_id,))
    row = cursor.fetchone()
    
    if not row:
        print(f"Bot {bot_id} NOT FOUND in DB.")
        return
        
    bid, name, active, config_json, strat_type, direction = row
    print(f"Bot ID: {bid}")
    print(f"Name: {name}")
    print(f"Active: {active}")
    print(f"Strategy: {strat_type}")
    print(f"Direction: {direction}")
    
    try:
        config = json.loads(config_json)
        print("\n--- CONFIGURATION ---")
        print(json.dumps(config, indent=2))
        
        # Check Price Trigger Specifics
        mode_price = config.get('mode_price', 0)
        price_thresh = config.get('price_threshold', 0.0)
        
        print("\n--- PRICE TRIGGER CHECK ---")
        print(f"mode_price: {mode_price} (Type: {type(mode_price)})")
        print(f"price_threshold: {price_thresh}")
        
        if int(mode_price) == 0:
            print("WARNING: mode_price is 0 (Disabled)!")
        elif int(mode_price) == 1:
            print("Mode: 1 (Price ABOVE Threshold)")
        elif int(mode_price) == 2:
            print("Mode: 2 (Price BELOW Threshold)")
            
    except Exception as e:
        print(f"Error parsing config: {e}")

if __name__ == "__main__":
    debug_bot(37)
