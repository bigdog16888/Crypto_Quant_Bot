import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("PRAGMA table_info(bots)")
for row in q.fetchall(): print(row)
q.execute("PRAGMA table_info(bot_settings)")
for row in q.fetchall(): print(row)
c.close()
