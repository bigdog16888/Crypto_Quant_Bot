import sqlite3
import json
conn = sqlite3.connect('crypto_bot.db', timeout=10)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT id, name, config FROM bots WHERE id = 10016")
row = cur.fetchone()
if row:
    cfg = json.loads(row['config'])
    print(f"Bot {row['name']} (ID {row['id']}):")
    for t in cfg.get('triggers', []):
        print(f"  Trigger: type={t.get('type')}, condition={t.get('condition')}, value={t.get('value')}")
conn.close()
