import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT timestamp, message FROM system_logs WHERE message LIKE '%PHYS-ADOPT%' ORDER BY id DESC LIMIT 50")
for r in q.fetchall():
    print(r[0], r[1])
c.close()
