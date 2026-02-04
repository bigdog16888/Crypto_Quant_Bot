import sys
import os
import sqlite3
import json

DB_PATH = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), "crypto_bot.db")

def check_triggers(bot_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT config FROM bots WHERE id = ?', (bot_id,))
    row = cursor.fetchone()
    
    if not row:
        print("Bot not found")
        return

    config = json.loads(row[0])
    
    print(f"--- Active Triggers for Bot {bot_id} ---")
    active_count = 0
    
    # Check standard modes
    for k, v in config.items():
        if k.startswith('mode_'):
            try:
                val = int(v)
                if val > 0:
                    print(f"[ACTIVE] {k}: {val}")
                    if k == 'mode_price':
                         print(f"   -> Threshold: {config.get('price_threshold', 'N/A')}")
                    active_count += 1
                else:
                    # print(f"[Inactive] {k}: 0")
                    pass
            except:
                pass
                
    # Check patterns
    for i in range(1, 5):
        k = f"pat_{i}_mode"
        val = config.get(k, 0)
        try:
            if int(val) > 0:
                 print(f"[ACTIVE] {k}: {val} (Source: {config.get(f'pat_{i}_source', 'Price')})")
                 active_count += 1
        except:
             pass
             
    print(f"Total Active Triggers: {active_count}")

if __name__ == "__main__":
    check_triggers(37)
