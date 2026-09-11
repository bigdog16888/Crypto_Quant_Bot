import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(bot_orders)')
rows = cursor.fetchall()
for row in rows:
    print(row)
print("---")
cursor.execute('PRAGMA table_info(trades)')
rows = cursor.fetchall()
for row in rows:
    print(row)
print("---")
cursor.execute('SELECT id, direction, status, hedge_child_bot_id FROM bots WHERE id IN (100001, 100324, 10019, 100319)')
for row in cursor.fetchall():
    print(row)
print("---")
cursor.execute('SELECT bot_id, open_qty, direction FROM trades WHERE bot_id IN (100001, 100324, 10019, 100319)')
for row in cursor.fetchall():
    print(row)
print("---")
cursor.execute('SELECT bot_id, order_type, status, amount, filled_amount, step, cycle_id, created_at, filled_at FROM bot_orders WHERE bot_id IN (100324, 100319) AND cycle_id = (SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id IN (100324, 100319))')
for row in cursor.fetchall():
    print(row)