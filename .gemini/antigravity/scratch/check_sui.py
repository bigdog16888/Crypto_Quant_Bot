import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT * FROM active_positions WHERE pair LIKE '%SUI%'")
for r in cur.fetchall():
    print(dict(r))
