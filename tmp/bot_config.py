import sqlite3
import json
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT config FROM bots WHERE id=10011")
row = q.fetchone()
if row:
    print(json.dumps(json.loads(row[0]), indent=2))
c.close()
