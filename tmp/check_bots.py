import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("SELECT id, pair, direction, is_active FROM bots WHERE pair IN ('SOLUSDC', 'BTCUSDC')")
for r in c.fetchall():
    print(r)
conn.close()
