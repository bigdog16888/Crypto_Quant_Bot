import sqlite3
import json

def fix_bot_configs():
    c = sqlite3.connect('crypto_bot.db')
    q = c.cursor()
    q.execute("SELECT id, direction, config FROM bots WHERE is_active=1")
    rows = q.fetchall()
    
    updates = 0
    for bot_id, db_dir, config_str in rows:
        if not config_str: continue
        try:
            config = json.loads(config_str)
            conf_dir = config.get('direction', '').upper()
            db_dir = db_dir.upper()
            if conf_dir != db_dir:
                print(f"Bot {bot_id} mismatch: DB={db_dir}, Config={conf_dir}. Fixing config -> {db_dir}")
                config['direction'] = db_dir
                q.execute("UPDATE bots SET config=? WHERE id=?", (json.dumps(config), bot_id))
                updates += 1
        except Exception as e:
            print(f"Error parsing config for bot {bot_id}: {e}")
            
    c.commit()
    c.close()
    print(f"Update complete. Fixed {updates} bots.")

if __name__ == "__main__":
    fix_bot_configs()
