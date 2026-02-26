import sqlite3
import json
import os

DB_PATH = os.path.join(os.getcwd(), "crypto_bot.db")

def check_configs():
    print("--- CHECKING BOT CONFIGS ---")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, config FROM bots WHERE is_active=1")
        bots = cursor.fetchall()
        
        for b in bots:
            bid, name, config_str = b
            try:
                cfg = json.loads(config_str)
                mt = cfg.get('market_type', 'UNKNOWN (Default Spot?)')
                print(f"Bot {bid} ({name}): market_type = {mt}")
            except:
                print(f"Bot {bid}: Config Parse Error")
        
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_configs()
