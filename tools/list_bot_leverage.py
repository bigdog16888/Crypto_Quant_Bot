
import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute('SELECT id, name, direction, config FROM bots WHERE is_active=1')
for r in cursor.fetchall():
    try:
        conf = json.loads(r[3])
        lev = conf.get('leverage', 'MISSING')
        print(f"Bot {r[0]} ({r[1]}) {r[2]}: Leverage={lev}")
    except:
        print(f"Bot {r[0]} ({r[1]}) {r[2]}: JSON Error")
conn.close()
