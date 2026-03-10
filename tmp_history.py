import sqlite3
import json

conn = sqlite3.connect('C:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT datetime(created_at, 'unixepoch') as ts, order_type, price, amount, status, client_order_id, filled_amount FROM bot_orders WHERE bot_id=10022 ORDER BY created_at DESC LIMIT 30")
rows = c.fetchall()

out = []
for r in rows:
    out.append(dict(r))
    
with open('orders.json', 'w') as f:
    json.dump(out, f, indent=2)
