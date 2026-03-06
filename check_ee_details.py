import sqlite3, json
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT config, target_tp_price, avg_entry_price FROM bots b JOIN trades t ON b.id = t.bot_id WHERE b.id = 10016')
row = c.fetchone()
config = json.loads(row[0])
interval = config.get('DecayIntervalMins')
decay_pc = config.get('DecayPercentPerInterval')
print(f'Bot 10016 | Interval: {interval}m | Decay %: {decay_pc}% | TP: {row[1]} | Avg Entry: {row[2]}')
