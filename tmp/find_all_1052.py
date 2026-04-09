import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT id, order_type, amount, filled_amount, created_at, status, client_order_id, cycle_id
    FROM bot_orders 
    WHERE bot_id=10018 AND (amount LIKE '1052.%' OR filled_amount LIKE '1052.%')
""")
for r in c.fetchall():
    print(r)

conn.close()
