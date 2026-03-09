import sqlite3
import json

def get_connection():
    return sqlite3.connect('crypto_bot.db', timeout=10.0)

bots_to_check = [10017, 10018]
conn = get_connection()
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

output = {}

for bot_id in bots_to_check:
    bot_info = {}
    
    # bot config (to see pair)
    cursor.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
    b = cursor.fetchone()
    if b:
        bot_info['bot'] = dict(b)
        
    # trades
    cursor.execute("SELECT * FROM trades WHERE bot_id = ?", (bot_id,))
    t = cursor.fetchone()
    if t:
        bot_info['trade'] = dict(t)
    
    # orders (ALL) to see what's missing
    cursor.execute("SELECT * FROM bot_orders WHERE bot_id = ? ORDER BY step ASC", (bot_id,))
    orders_rows = cursor.fetchall()
    bot_info['orders'] = [dict(o) for o in orders_rows]
    
    output[f'bot_{bot_id}'] = bot_info

# Export
with open('mismatch_debug_dump.json', 'w') as f:
    json.dump(output, f, indent=4)
    
print("Dumped to mismatch_debug_dump.json")
