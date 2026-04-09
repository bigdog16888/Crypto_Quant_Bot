import sqlite3, time

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

q.execute("SELECT id, name, direction FROM bots WHERE pair='SUIUSDC' AND is_active=1")
print('SUI bots:', q.fetchall())

q.execute("SELECT bot_id, total_invested, avg_entry_price, cycle_id, entry_confirmed FROM trades WHERE bot_id IN (SELECT id FROM bots WHERE pair='SUIUSDC')")
print('SUI trades:', q.fetchall())

q.execute("SELECT bot_id, pair, side, size, entry_price FROM active_positions WHERE pair='SUIUSDC'")
print('SUI active_pos:', q.fetchall())

q.execute("SELECT order_type,status,filled_amount,cycle_id,client_order_id FROM bot_orders WHERE bot_id IN (SELECT id FROM bots WHERE pair='SUIUSDC') AND order_type='adoption' AND status='filled'")
print('SUI adoptions:', q.fetchall())
c.close()
