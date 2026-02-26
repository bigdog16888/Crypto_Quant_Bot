
import sqlite3
import json
import random

# Target Price Logic:
# BTC ~ 67000. 
# Long Bots need Price < Threshold (Mode 2). So set Threshold to 70000.
# Short Bots need Price > Threshold (Mode 1). So set Threshold to 60000.

def force_activate():
    print("--- FORCING BOT TRIGGERS (LIVE TEST) ---")
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, name, pair, config, direction FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    
    updated = 0
    for b in bots:
        bid, name, pair, cfg_json, direction = b
        try:
            cfg = json.loads(cfg_json)
            
            # FORCE VALID THRESHOLD
            if direction == 'LONG':
                # Mode 2 (Price < X). To trigger, X must be > Current.
                # BTC is ~67k. Let's set X = 999,999 (Always Buy)
                cfg['mode_price'] = 2
                cfg['price_threshold'] = 999999.0 
            else:
                # Mode 1 (Price > X). To trigger, X must be < Current.
                # BTC is ~67k. Let's set X = 1.0 (Always Sell)
                cfg['mode_price'] = 1
                cfg['price_threshold'] = 1.0
            
            new_json = json.dumps(cfg)
            cursor.execute("UPDATE bots SET config=? WHERE id=?", (new_json, bid))
            updated += 1
            print(f"✅ Updated {name}: FORCE TRIGGER ENABLED")
            
        except Exception as e:
            print(f"❌ Failed to update {name}: {e}")

    conn.commit()
    conn.close()
    print(f"✅ Updated {updated} bots. They should trigger within 10 seconds.")

if __name__ == "__main__":
    force_activate()
