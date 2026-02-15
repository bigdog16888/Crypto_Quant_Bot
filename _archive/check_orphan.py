"""Check what's actually in the database tables"""
from engine.database import get_connection

conn = get_connection()
cur = conn.cursor()

# List all tables
print('=== DATABASE TABLES ===')
for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    print(r[0])

# Check the trades_history or similar
print('\n=== CHECKING TRADES TABLE STRUCTURE ===')
cur.execute("PRAGMA table_info(trades)")
for col in cur.fetchall():
    print(f"  {col[1]} ({col[2]})")

print('\n=== CHECKING BOT_ORDERS TABLE ===')
cur.execute("PRAGMA table_info(bot_orders)")
for col in cur.fetchall():
    print(f"  {col[1]} ({col[2]})")

print('\n=== ALL FILLED ORDERS (Not just open) ===')
for r in cur.execute('''
    SELECT bot_id, order_type, order_id, price, amount, status, created_at
    FROM bot_orders
    WHERE status = 'filled'
    ORDER BY created_at DESC
    LIMIT 30
''').fetchall():
    qty = r[4]
    price = r[3]
    cost = qty * price if qty and price else 0
    print(f'Bot {r[0]}: {r[1]:5} | Price=${r[3]:.2f} | Qty={r[4]:.6f} | ${cost:.2f} | {r[5]}')

print('\n=== ORPHAN CHECK: Exchange position vs DB ===')
print('DB Bot 41 thinks: 0.002487 BTC (~$181)')
print('DB Bot 43 thinks: 0.002474 BTC (~$180)')
print('DB TOTAL: ~0.005 BTC (~$361)')
print('')
print('Exchange ACTUAL: 0.578 BTC (~$42,840)')
print('')
print('DIFFERENCE: 0.573 BTC (~$42,479) ORPHAN!')
print('')
print('This orphan position was NOT created by the bots!')
print('Possible causes:')
print('  1. Manual trades on the exchange')
print('  2. Bot runaway before tracking was implemented')
print('  3. Grid fills not updating DB state')
