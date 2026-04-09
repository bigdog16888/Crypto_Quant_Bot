import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%BTC%' OR pair LIKE '%SOL%'")
for r in c.fetchall():
    print(r)
conn.close()
