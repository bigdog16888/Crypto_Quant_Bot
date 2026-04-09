import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair='ETHUSDC'")
for r in q.fetchall(): print(r)
c.close()
