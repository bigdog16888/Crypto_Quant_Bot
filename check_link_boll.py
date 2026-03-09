import sqlite3, json
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute("SELECT id, name, config FROM bots WHERE name LIKE '%link%' OR name LIKE '%LINK%'")
rows = c.fetchall()
for r in rows:
    cfg = json.loads(r[2]) if r[2] else {}
    print(f"Bot {r[0]} ({r[1]}):")
    for k, v in cfg.items():
        if 'boll' in k.lower():
            print(f"  {k} = {v}")
conn.close()
