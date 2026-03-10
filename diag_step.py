import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("PRAGMA table_info(bot_orders)")
print("bot_orders:", [r[1] for r in c.fetchall()])

c.execute("PRAGMA table_info(bots)")
print("bots:", [r[1] for r in c.fetchall()])

c.execute("SELECT created_at, step, order_type, amount, status FROM bot_orders WHERE bot_id=10020 AND status IN ('filled','closed') AND order_type IN ('entry', 'grid') ORDER BY created_at DESC LIMIT 10")
for r in c.fetchall():
    print(r)
