import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("""
    SELECT cycle_id, filled_amount, order_type, datetime(created_at, 'unixepoch', 'localtime') as t 
    FROM bot_orders 
    WHERE bot_id=10018 
    ORDER BY created_at DESC 
    LIMIT 20
""")

for r in c.fetchall():
    print(r)

conn.close()
