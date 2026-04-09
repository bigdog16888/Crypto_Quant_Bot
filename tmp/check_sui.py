import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("PRAGMA table_info(bot_orders)")
for r in q.fetchall(): print(r)
q.execute("SELECT id, bot_id, status, filled_amount, created_at FROM bot_orders WHERE bot_id=10018 AND order_type='tp' ORDER BY created_at DESC LIMIT 10")
for r in q.fetchall(): print(r)
c.close()
