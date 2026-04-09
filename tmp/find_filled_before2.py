import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT order_type, amount, filled_amount, created_at, client_order_id, status 
    FROM bot_orders 
    WHERE bot_id=10018 AND filled_amount > 0 AND created_at <= 1774914031
    ORDER BY created_at DESC LIMIT 20
""")
for r in c.fetchall():
    print(r)

conn.close()
