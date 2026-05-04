import sqlite3
conn = sqlite3.connect('crypto_bot.db', timeout=10)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT name, status, last_error FROM bots WHERE id = 10016")
row = cur.fetchone()
if row:
    print(f"Bot {row['name']} | Status: {row['status']} | Last Error: {row['last_error']}")
conn.close()
