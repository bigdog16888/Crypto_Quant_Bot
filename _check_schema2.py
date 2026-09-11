import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('PRAGMA table_info(bots)')
for r in cur.fetchall():
    print(r)
conn.close()