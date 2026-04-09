import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT id, order_type, amount, filled_amount, status, created_at FROM bot_orders WHERE bot_id=10018 AND amount=1052.7")
for r in c.fetchall():
    print(r)
    
conn.close()
