import sqlite3

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
print('=== Bots for BTC/USDC:USDC ===')
c.execute("""
    SELECT id, pair, direction, status, is_active
    FROM bots
    WHERE pair = 'BTC/USDC:USDC'
    ORDER BY id
""")
bots = c.fetchall()
for row in bots:
    print(row)

print()
print('=== Bot orders for BTC/USDC bots ===')
bot_ids = [row[0] for row in bots]
if bot_ids:
    placeholders = ','.join('?' for _ in bot_ids)
    c.execute(f"""
        SELECT id, bot_id, order_type, amount, filled_amount, status, created_at
        FROM bot_orders
        WHERE bot_id IN ({placeholders})
        ORDER BY bot_id, id
    """, bot_ids)
    for row in c.fetchall():
        print(row)
else:
    print('No bots found')

print()
print('=== Trades for BTC/USDC bots ===')
if bot_ids:
    placeholders = ','.join('?' for _ in bot_ids)
    c.execute(f"""
        SELECT id, bot_id, position_side, open_qty, avg_entry_price, current_step, cycle_id, cycle_phase, close_type
        FROM trades
        WHERE bot_id IN ({placeholders})
        ORDER BY bot_id, id
    """, bot_ids)
    for row in c.fetchall():
        print(row)
else:
    print('No trades found')

print()
print('=== Exchange fills for BTC/USDC (should be empty) ===')
c.execute("""
    SELECT id, bot_id, symbol, side, qty, price, source, cycle_id, step, client_order_id, exchange_order_id
    FROM exchange_fills
    WHERE symbol IN ('BTCUSDC', 'BTC/USDC:USDC')
    ORDER BY id
""")
fills = c.fetchall()
if fills:
    for row in fills:
        print(row)
else:
    print('No exchange fills for BTC/USDC symbols')

print()
print('=== Net position calculation ===')
# From unowned_position_alerts
exchange_sum = sum(row[2] for row in alerts)
db_sum = sum(row[3] for row in alerts)
print(f'Sum exchange_qty: {exchange_sum:.8f}')
print(f'Sum db_qty: {db_sum:.8f}')
print(f'Orphan (exchange - db): {exchange_sum - db_sum:.8f}')

# From trades (if we trust the open_qty and position_side)
print()
print('=== Position from trades (open_qty * side) ===')
if bot_ids:
    placeholders = ','.join('?' for _ in bot_ids)
    c.execute(f"""
        SELECT bot_id, position_side, open_qty
        FROM trades
        WHERE bot_id IN ({placeholders})
    """, bot_ids)
    trade_pos = 0.0
    for bot_id, pos_side, open_qty in c.fetchall():
        # position_side is either 'LONG' or 'SHORT' or 'BOTH'
        # We assume that if position_side is 'LONG', open_qty is long; if 'SHORT', open_qty is short (negative)
        # But note: the trades table might have open_qty as absolute and position_side indicates the side.
        # Let's assume open_qty is non-negative and position_side tells us the sign.
        if pos_side == 'LONG':
            signed = open_qty
        elif pos_side == 'SHORT':
            signed = -open_qty
        else:
            # For 'BOTH', we don't know, but we can skip or assume 0? Let's look at the data.
            signed = 0.0  # placeholder
        trade_pos += signed
        print(f'Bot {bot_id}: position_side={pos_side}, open_qty={open_qty:.8f}, signed={signed:.8f}')
    print(f'Total position from trades: {trade_pos:.8f}')
else:
    print('No trades to calculate')

conn.close()