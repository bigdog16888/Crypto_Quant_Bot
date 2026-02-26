import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
print('=== ACTIVE POSITIONS (Physical) ===')
rows = conn.execute(
    "SELECT pair, side, size, entry_price, datetime(last_checked, 'unixepoch', 'localtime') FROM active_positions"
).fetchall()
for r in rows:
    print(f'  {r[0]} {r[1]} size={r[2]} @ {r[3]} (updated {r[4]})')
if not rows:
    print('  EMPTY - no physical positions recorded')

print()
print('=== VIRTUAL NET per pair ===')
virt = conn.execute("""
    SELECT b.pair,
           SUM(CASE WHEN b.direction='LONG' THEN t.total_invested ELSE -t.total_invested END) as net_usd
    FROM trades t JOIN bots b ON t.bot_id=b.id
    WHERE b.is_active=1 AND t.total_invested > 0
    GROUP BY b.pair
""").fetchall()
for r in virt:
    print(f'  {r[0]}: virtual_net=${r[1]:.2f}')

print()
print('=== PHYSICAL NET per pair ===')
phys = conn.execute("""
    SELECT pair,
           SUM(CASE WHEN side='LONG' THEN size*entry_price ELSE -(size*entry_price) END) as net_usd
    FROM active_positions
    GROUP BY pair
""").fetchall()
for r in phys:
    print(f'  {r[0]}: physical_net=${r[1]:.2f}')

print()
print('=== STALE ORDER IDs (should be 0 for zero-invested bots) ===')
stale = conn.execute("""
    SELECT b.name, t.total_invested, t.entry_order_id, t.tp_order_id
    FROM trades t JOIN bots b ON t.bot_id=b.id
    WHERE t.total_invested=0 AND (t.entry_order_id IS NOT NULL OR t.tp_order_id IS NOT NULL)
""").fetchall()
for r in stale:
    print(f'  {r[0]}: invested=0 but entry_id={r[2]} tp_id={r[3]}')
if not stale:
    print('  CLEAN - no stale order IDs')

now = time.time()
print()
print(f'=== ACTIVE POSITIONS last_checked age ===')
age_rows = conn.execute("SELECT pair, last_checked FROM active_positions").fetchall()
for r in age_rows:
    age = now - r[1]
    print(f'  {r[0]}: {age:.0f}s ago')

conn.close()
