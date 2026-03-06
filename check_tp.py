import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("SELECT id, order_id, order_type, price, amount, status FROM bot_orders WHERE bot_id = 10016 AND order_type = 'tp' AND status = 'open'")
rows = c.fetchall()
print(f"Open TP Orders for 10016: {len(rows)}")
for r in rows:
    print(r)
