import sqlite3
conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Full breakdown of bot 10019 cycle 2 orders that count toward net
cur.execute('''SELECT id, order_type, price, amount, filled_amount, status, notes
FROM bot_orders WHERE bot_id=10019 AND cycle_id=2 AND filled_amount>0
AND status NOT IN ('reset_cleared','auto_closed')
ORDER BY id ASC''')
print('=== Filled orders for bot 10019 cycle 2 ===')
total_buy = 0
total_sell = 0
for r in cur.fetchall():
    d = dict(r)
    side = 'BUY' if d['order_type'] in ('entry','grid','adoption','adoption_add') else 'SELL'
    print(f"  id={d['id']} type={d['order_type']:15s} price={d['price']:8.2f} filled={d['filled_amount']} status={d['status']} notes={d['notes']}")
    if d['order_type'] in ('entry','grid','adoption','adoption_add'):
        total_buy += d['filled_amount']
    else:
        total_sell += d['filled_amount']
print(f'\nTotal BUY qty: {total_buy}')
print(f'Total SELL qty: {total_sell}')
print(f'Net open qty from orders: {round(total_buy - total_sell, 8)}')
print()

# What does trades table say
cur.execute('SELECT open_qty, total_invested, avg_entry_price, cycle_id FROM trades WHERE bot_id=10019')
t = dict(cur.fetchone())
print(f"trades.open_qty={t['open_qty']}, total_invested={t['total_invested']}, avg_entry={t['avg_entry_price']}")

# Why does the TP keep getting cancelled? Check the most recent tp_order_id on exchange
cur.execute("SELECT * FROM trades WHERE bot_id=10019")
tr = dict(cur.fetchone())
print(f"\ntp_order_id in trades table: {tr['tp_order_id']}")

# Count current 'new' TP orders
cur.execute("SELECT id, order_id, price, amount, status, created_at FROM bot_orders WHERE bot_id=10019 AND order_type='tp' AND status IN ('new','open','placed') ORDER BY created_at DESC LIMIT 5")
print("\n=== Live TP orders (new/open) ===")
for r in cur.fetchall():
    print(f"  id={r['id']} order_id={r['order_id']} price={r['price']} qty={r['amount']} status={r['status']}")

conn.close()
