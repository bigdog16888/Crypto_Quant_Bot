import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT id, order_type, order_id, status, amount, filled_amount FROM bot_orders WHERE order_id IN ('84663310', '84638706')")
print(c.fetchall())

conn.close()
