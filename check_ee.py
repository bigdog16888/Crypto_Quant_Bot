import sqlite3, time, json
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT b.id, b.name, b.config, t.basket_start_time FROM bots b JOIN trades t ON b.id = t.bot_id WHERE t.total_invested > 0')
now = time.time()
for row in c.fetchall():
  config = json.loads(row[2])
  ee_start = config.get('EarlyExitStartHours', 0)
  age = (now - row[3]) / 3600
  print(f'Bot {row[0]} ({row[1]}): Age={age:.2f}h, EE_Start={ee_start}h, Eligible={age > ee_start}')
