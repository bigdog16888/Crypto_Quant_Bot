import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

cur.execute('SELECT id, name, is_active, base_size, config FROM bots WHERE id=43')
row = cur.fetchone()

bot_id, name, is_active, base_size_db, config_str = row

print(f"Bot #{bot_id}: {name}")
print(f"Active (DB): {is_active}")
print(f"Base Size (DB column): {base_size_db}")

config = json.loads(config_str)
print(f"Base Size (config): {config.get('base_size')}")
print()

if not is_active:
    print("⚠️  Bot is INACTIVE in database!")
    print("   The UI might be showing a cached state.")
    print("   Try refreshing the UI or restarting the engine.")

conn.close()
