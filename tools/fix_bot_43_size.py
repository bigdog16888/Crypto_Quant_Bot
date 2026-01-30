import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

bot_id = 43
new_size = 200.0

# Update base_size column
cur.execute('UPDATE bots SET base_size = ? WHERE id = ?', (new_size, bot_id))

# Update config json
cur.execute('SELECT config FROM bots WHERE id = ?', (bot_id,))
row = cur.fetchone()
if row:
    config = json.loads(row[0])
    config['base_size'] = new_size
    new_config_str = json.dumps(config)
    cur.execute('UPDATE bots SET config = ? WHERE id = ?', (new_config_str, bot_id))
    print(f"✅ Updated Bot #{bot_id} base_size to ${new_size}")

conn.commit()
conn.close()
