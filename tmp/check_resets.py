import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT bot_id, action, timestamp, notes FROM trade_history WHERE bot_id IN (10016, 10011, 10021, 100002) ORDER BY id DESC LIMIT 30")
for r in q.fetchall():
    print(r)
c.close()
