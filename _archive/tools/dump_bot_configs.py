
import sqlite3
import json

def dump_configs():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, pair, config FROM bots WHERE is_active=1")
    bots = cursor.fetchall()
    conn.close()
    
    print("--- ACTIVE BOT CONFIGS ---")
    for b in bots:
        bid, name, pair, cfg_json = b
        try:
            cfg = json.loads(cfg_json)
            mode = cfg.get('mode_price', 'UNK')
            thresh = cfg.get('price_threshold', 'UNK')
            print(f"[{bid}] {name} ({pair}) | Mode={mode} Thresh={thresh}")
        except:
            print(f"[{bid}] {name} - Bad Config")

if __name__ == "__main__":
    dump_configs()
