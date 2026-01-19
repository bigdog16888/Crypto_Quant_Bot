import sqlite3
import json
from config.settings import config

def update_bot_market_type(bot_id, market_type):
    conn = sqlite3.connect(config.PATHS["DB_FILE"])
    cursor = conn.cursor()
    
    # Get current config
    cursor.execute("SELECT config FROM bots WHERE id = ?", (bot_id,))
    row = cursor.fetchone()
    if row:
        config_dict = json.loads(row[0]) if row[0] else {}
        config_dict['market_type'] = market_type
        new_config_json = json.dumps(config_dict)
        
        cursor.execute("UPDATE bots SET config = ? WHERE id = ?", (new_config_json, bot_id))
        conn.commit()
        print(f"Updated Bot #{bot_id} to market_type={market_type}")
    else:
        print(f"Bot #{bot_id} not found.")
    
    conn.close()

if __name__ == "__main__":
    update_bot_market_type(2, 'spot')
