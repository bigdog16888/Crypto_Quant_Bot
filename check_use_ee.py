import sqlite3, json
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT config FROM bots WHERE id = 10016')
row = c.fetchone()
if row:
    config = json.loads(row[0])
    print(f"UseEarlyExit: {config.get('UseEarlyExit')}")
else:
    print("Bot 10016 not found.")
