import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT id, order_type, amount, filled_amount, created_at, client_order_id, status 
    FROM bot_orders 
    WHERE bot_id=10018 AND amount > 40000
""")
for r in c.fetchall():
    print(r)

conn.close()
