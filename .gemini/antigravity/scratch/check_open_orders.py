import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT id, bot_id, client_order_id, status FROM bot_orders WHERE status IN ('open', 'new', 'placing')")
for r in cur.fetchall():
    print(dict(r))
conn.close()
