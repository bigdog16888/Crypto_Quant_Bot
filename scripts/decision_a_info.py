import sqlite3
import os

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== BTC/USDC unowned_position_alerts ===')
c.execute("""
    SELECT id, bot_id, exchange_qty, db_qty, detected_at, status, notes
    FROM unowned_position_alerts
    WHERE normalized_pair = 'BTCUSDC'
    ORDER BY id
""")
alerts = c.fetchall()
for row in alerts:
    print(row)

print()
print('=== Summary ===')
exchange_sum = sum(row[2] for row in alerts)
db_sum = sum(row[3] for row in alerts)
print(f'Sum exchange_qty: {exchange_sum:.8f}')
print(f'Sum db_qty: {db_sum:.8f}')
print(f'Orphan (exchange - db): {exchange_sum - db_sum:.8f} BTC')

print()
print('=== Related bots (BTC/USDC:USDC) ===')
c.execute("""
    SELECT id, pair, direction, status, is_active
    FROM bots
    WHERE pair = 'BTC/USDC:USDC'
    ORDER BY id
""")
for row in c.fetchall():
    print(row)

print()
print('=== Test bots that might be involved (numeric hedge bots) ===')
c.execute("""
    SELECT id, pair, direction, bot_type, status
    FROM bots
    WHERE pair = 'BTC/USDC:USDC' AND bot_type LIKE '%hedge%'
    ORDER BY id
""")
for row in c.fetchall():
    print(row)

print()
print('=== Recent exchange_fills for BTC/USDC (should be empty post-purge) ===')
c.execute("""
    SELECT id, bot_id, symbol, side, qty, price, source, cycle_id, step, client_order_id, exchange_order_id, fill_ts
    FROM exchange_fills
    WHERE symbol IN ('BTCUSDC', 'BTC/USDC:USDC')
    ORDER BY id DESC LIMIT 5
""")
rows = c.fetchall()
if rows:
    for row in rows:
        print(row)
else:
    print('No exchange fills for BTC/USDC symbols')

conn.close()