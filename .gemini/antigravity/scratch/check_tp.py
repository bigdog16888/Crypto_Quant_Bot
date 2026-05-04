import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id=10018 AND cycle_id=12 AND order_type IN ('tp','close','dust_close') AND status IN ('filled','closed','reset_cleared') AND filled_amount > 0")
print('TP count:', cur.fetchone()[0])
conn.close()
