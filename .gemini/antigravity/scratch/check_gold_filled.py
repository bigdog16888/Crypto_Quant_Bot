import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT id, status, order_type, filled_amount, created_at, client_order_id, notes FROM bot_orders WHERE bot_id=10019 AND filled_amount > 0 ORDER BY id DESC LIMIT 5")
for r in cur.fetchall():
    print(dict(r))
conn.close()
