
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute('PRAGMA table_info(trades)')
cols = cur.fetchall()
with open('trades_schema.txt', 'w') as f:
    for col in cols:
        f.write(str(col) + '\n')
conn.close()
