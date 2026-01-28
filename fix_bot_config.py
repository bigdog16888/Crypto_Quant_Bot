
import sqlite3
import json
import logging

logging.basicConfig(level=logging.INFO)

def fix_bot_43():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    # Get Bot 43
    cur.execute("SELECT config FROM bots WHERE id=43")
    row = cur.fetchone()
    
    if not row:
        print("Bot 43 not found!")
        return
        
    config = json.loads(row[0])
    print(f"Old Config Mode: {config.get('mode_price')}")
    
    # Update Mode to 1 (Above)
    config['mode_price'] = 1
    # Threshold is already 87000
    
    # Save back
    new_config_str = json.dumps(config)
    cur.execute("UPDATE bots SET config=? WHERE id=43", (new_config_str,))
    conn.commit()
    conn.close()
    
    print(f"New Config Mode: {config.get('mode_price')} (1=Above)")
    print("✅ Bot 43 Updated successfully.")

if __name__ == "__main__":
    fix_bot_43()
