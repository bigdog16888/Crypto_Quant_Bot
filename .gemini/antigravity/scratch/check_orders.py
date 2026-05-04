import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
print("--- Bot 10019 bot_orders ---")
cur.execute("SELECT id, order_type, filled_amount, status, client_order_id, cycle_id, created_at, filled_at FROM bot_orders WHERE bot_id=10019 AND status NOT IN ('reset_cleared', 'auto_closed', 'cancelled', 'canceled')")
for r in cur.fetchall():
    print(dict(r))

print("--- Bot 10018 bot_orders ---")
cur.execute("SELECT id, order_type, filled_amount, status, client_order_id, cycle_id, created_at, filled_at FROM bot_orders WHERE bot_id=10018 AND status NOT IN ('reset_cleared', 'auto_closed', 'cancelled', 'canceled')")
for r in cur.fetchall():
    print(dict(r))
conn.close()
