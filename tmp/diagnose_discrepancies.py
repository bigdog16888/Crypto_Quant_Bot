import sqlite3

conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()

# Check active_positions schema
cursor.execute('PRAGMA table_info(active_positions)')
print('=== active_positions columns ===')
for row in cursor.fetchall():
    print(row)

print()
print('=== ACTIVE_POSITIONS ===')
cursor.execute('SELECT * FROM active_positions LIMIT 20')
for row in cursor.fetchall():
    print(row)

print()
print('=== KEY BOTS WITH DISCREPANCIES - bot_orders ===')
for bot_id in [10008, 10017, 10018, 10016, 100000]:
    cursor.execute("""
        SELECT bot_id, order_type, step, filled_amount, status, cycle_id
        FROM bot_orders
        WHERE bot_id=? AND filled_amount>0 AND status NOT IN ('reset_cleared', 'auto_closed', 'failed', 'placing')
        ORDER BY created_at
    """, (bot_id,))
    rows = cursor.fetchall()
    print(f'Bot {bot_id}: {len(rows)} orders')
    for r in rows:
        print(f'  {r}')

print()
print('=== RECOMPUTED INVESTED from bot_orders ===')
cursor.execute("""
    SELECT bo.bot_id, b.name, b.direction, t.cycle_id,
        SUM(CASE WHEN bo.order_type IN ('entry','grid','adoption_add','adoption') THEN bo.filled_amount ELSE 0 END) as entry_qty,
        SUM(CASE WHEN bo.order_type IN ('tp','close','exit','adoption_reduce','dust_close','sl') THEN bo.filled_amount ELSE 0 END) as exit_qty,
        SUM(CASE WHEN bo.order_type IN ('entry','grid','adoption_add','adoption') THEN bo.filled_amount * bo.price ELSE 0 END) as entry_cost
    FROM bots b
    JOIN trades t ON b.id = t.bot_id
    JOIN bot_orders bo ON b.id = bo.bot_id AND bo.filled_amount > 0
        AND bo.status NOT IN ('reset_cleared','auto_closed','failed','placing')
    WHERE b.is_active = 1
    GROUP BY bo.bot_id
""")
for row in cursor.fetchall():
    bot_id, name, direction, cycle_id, entry_qty, exit_qty, entry_cost = row
    net_qty = entry_qty - exit_qty
    print(f"  Bot {bot_id} ({name} {direction}) cycle={cycle_id}: entry_qty={entry_qty:.4f} exit_qty={exit_qty:.4f} net_qty={net_qty:.4f} entry_cost=${entry_cost:.2f}")

print()
print('=== trades table total_invested vs bot_orders recompute ===')
cursor.execute("""
    SELECT b.id, b.name, b.direction, t.total_invested, t.current_step, t.cycle_id
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE b.is_active=1 AND t.total_invested > 0
""")
for row in cursor.fetchall():
    print(f"  {row}")

conn.close()
