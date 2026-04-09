import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT order_type, SUM(filled_amount) FROM bot_orders WHERE bot_id=10020 AND filled_amount > 0 GROUP BY order_type")
for r in q.fetchall(): print(r)
c.close()
