from engine.database import get_connection
import json

conn = get_connection()
c = conn.cursor()

print('=== SUI GRID_9 details ===')
c.execute('''SELECT id, price, amount, filled_amount, status, client_order_id
             FROM bot_orders WHERE bot_id=10018 AND cycle_id=36 AND step=9 AND order_type='grid'
             ORDER BY id''')
for r in c.fetchall():
    print(f'  id={r[0]} price={r[1]} amount={r[2]} filled={r[3]} status={r[4]} cid={r[5]}')

print()
print('=== SUI cycle 36 filled totals by step ===')
c.execute('''SELECT step, order_type, SUM(filled_amount), status
             FROM bot_orders WHERE bot_id=10018 AND cycle_id=36
             GROUP BY step, order_type, status ORDER BY step, order_type''')
for r in c.fetchall():
    if r[2] and float(r[2]) > 0:
        print(f'  step={r[0]} {r[1]:10s} {r[3]:15s} total_fill={r[2]:.4f}')

print()
print('=== SUI pos_limit_hit flag in bots table ===')
c.execute('SELECT id, name, pos_limit_hit FROM bots WHERE id=10018')
b = c.fetchone()
if b:
    print(f'  id={b[0]} name={b[1]} pos_limit_hit={b[2]}')

print()
print('=== XRP cycle 40 complete order history ===')
c.execute('''SELECT order_type, status, filled_amount, step, amount
             FROM bot_orders WHERE bot_id=10017 AND cycle_id=40
             ORDER BY step, id''')
rows = c.fetchall()
total_fill = 0.0
for r in rows:
    fill = float(r[2] or 0)
    sign = -1 if r[0] == 'tp' else 1
    print(f'  step={r[3]} {r[0]:6s} {r[1]:15s} amount={r[4]} fill={r[2]}')
print()
print('=== XRP net fills (entries - TPs) ===')
c.execute('''SELECT order_type, SUM(filled_amount)
             FROM bot_orders WHERE bot_id=10017 AND cycle_id=40
             GROUP BY order_type ORDER BY order_type''')
for r in c.fetchall():
    print(f'  {r[0]:6s}: total_fill={r[1]:.4f}')
