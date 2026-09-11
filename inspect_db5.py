import sqlite3
conn = sqlite3.connect('crypto_bot.db.backup_20260807_153057')
cursor = conn.cursor()

print("=== bots ===")
cursor.execute('SELECT id, direction, status, hedge_child_bot_id FROM bots WHERE id IN (100001, 100324, 10019, 100319)')
for row in cursor.fetchall():
    print(row)

print("=== trades ===")
cursor.execute('SELECT bot_id, open_qty, direction FROM trades WHERE bot_id IN (100001, 100324, 10019, 100319)')
for row in cursor.fetchall():
    print(row)

print("=== bot_orders for child 100324 (SOL hedge) ===")
cursor.execute('''SELECT bot_id, order_type, status, amount, filled_amount, step, cycle_id, created_at, filled_at 
                  FROM bot_orders WHERE bot_id = 100324 AND cycle_id = (SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id = 100324)''')
for row in cursor.fetchall():
    print(row)

print("=== bot_orders for child 100319 (gold hedge) ===")
cursor.execute('''SELECT bot_id, order_type, status, amount, filled_amount, step, cycle_id, created_at, filled_at 
                  FROM bot_orders WHERE bot_id = 100319 AND cycle_id = (SELECT MAX(cycle_id) FROM bot_orders WHERE bot_id = 100319)''')
for row in cursor.fetchall():
    print(row)

print("=== bot_orders schema ===")
cursor.execute('PRAGMA table_info(bot_orders)')
for row in cursor.fetchall():
    print(row)