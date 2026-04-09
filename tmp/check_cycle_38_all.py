import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT order_type, amount, filled_amount, status FROM bot_orders WHERE bot_id=10018 AND cycle_id=38")
for r in c.fetchall():
    print(r)

conn.close()
