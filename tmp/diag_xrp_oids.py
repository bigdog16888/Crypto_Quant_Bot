import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT * FROM bot_orders WHERE order_id IN ('84663310', '84638706')")
rows = c.fetchall()

if rows:
    print("Found in bot_orders:")
    for r in rows:
        print(r)
else:
    print("These OIDs do NOT exist in bot_orders AT ALL.")

conn.close()
