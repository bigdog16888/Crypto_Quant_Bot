import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT bot_id, filled_amount FROM bot_orders WHERE symbol='ETHUSDC' AND filled_amount > 0 AND created_at > 1774850000")
for r in q.fetchall(): print(r)
c.close()
