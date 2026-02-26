import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
print('=== BOT_ORDERS (open) ===')
rows = conn.execute(
    "SELECT id, bot_id, order_id, order_type, status, created_at, price FROM bot_orders WHERE status='open' ORDER BY created_at DESC LIMIT 20"
).fetchall()
for r in rows:
    age = time.time() - r[5] if r[5] else 0
    print(f'  db_id={r[0]} bot={r[1]} exch_id={r[2]} type={r[3]} age={age:.0f}s price={r[6]}')

print()
print('=== TRADES entry/tp order ids ===')
rows2 = conn.execute('SELECT bot_id, entry_order_id, tp_order_id FROM trades').fetchall()
for r in rows2:
    if r[1] or r[2]:
        print(f'  bot={r[0]} entry={r[1]} tp={r[2]}')

print()
print('Protection check: grid orders in bot_orders NOT protected by fix_stuck_orders?')
protected_ids = set()
for r in rows2:
    if r[1]: protected_ids.add(str(r[1]))
    if r[2]: protected_ids.add(str(r[2]))

for r in rows:
    ex_id = str(r[2])
    is_grid = r[3] == 'grid'
    is_protected = ex_id in protected_ids
    age = time.time() - r[5] if r[5] else 0
    if is_grid and not is_protected and age > 60:
        print(f'  ❌ UNPROTECTED GRID: bot={r[1]} exch_id={ex_id} age={age:.0f}s → will be ORPHAN-KILLED!')
    elif is_grid and not is_protected:
        print(f'  ⏳ New grid (< 60s grace): bot={r[1]} exch_id={ex_id} age={age:.0f}s → safe for now')

conn.close()
