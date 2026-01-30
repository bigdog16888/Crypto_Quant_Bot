import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

bot_id = 43
new_size = 180.0

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

# Check for open orders that might block
print("\nChecking for open orders...")
# This would require CCXT, let's just use the DB trade status
cur.execute('SELECT current_step, total_invested FROM trades WHERE bot_id = ?', (bot_id,))
trade = cur.fetchone()
if trade:
    print(f"Trade State: Step {trade[0]}, Invested ${trade[1]}")
    if trade[1] > 0:
        print("⚠️ Bot thinks it has a position (Invested > 0). This might prevent new entry.")

conn.commit()
conn.close()
