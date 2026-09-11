import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT id, name, pair, normalized_pair, direction, is_active, status FROM bots WHERE id=10007')
print('Bot 10007:', c.fetchone())
c.execute('SELECT * FROM trades WHERE bot_id=10007')
print('Trades 10007:', c.fetchone())
c.execute('SELECT * FROM bot_orders WHERE bot_id=10007 AND filled_amount > 0 ORDER BY created_at DESC LIMIT 20')
print('Orders 10007:', c.fetchall())