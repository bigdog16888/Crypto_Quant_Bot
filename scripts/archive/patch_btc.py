import sqlite3, time
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
bot_id = 10015
gap = 0.003
c.execute("INSERT INTO bot_orders (bot_id, order_type, filled_amount, status, created_at, cycle_id, position_side) VALUES (?, 'forensic_adoption', ?, 'filled', ?, 3, 'SHORT')", (bot_id, gap, int(time.time())))
conn.commit()
print('Adopted gap of', gap, 'to bot', bot_id)
