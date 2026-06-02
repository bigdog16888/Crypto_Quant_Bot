import sqlite3

db = sqlite3.connect('crypto_bot.db')
db.row_factory = sqlite3.Row

print("=== QUERY 1 ===")
rows1 = db.execute("""
SELECT t.bot_id, b.name, t.cycle_id, t.entry_confirmed, t.current_step, t.total_invested
FROM trades t JOIN bots b ON t.bot_id = b.id
WHERE b.name LIKE '%xrp%';
""").fetchall()
for r in rows1:
    print(f"  bot_id={r['bot_id']} | name={r['name']:20s} | cycle={r['cycle_id']} | entry_confirmed={r['entry_confirmed']} | step={r['current_step']} | invested={r['total_invested']}")

print("\n=== QUERY 2 ===")
rows2 = db.execute("""
SELECT cycle_id, order_type, status, filled_amount, client_order_id
FROM bot_orders 
WHERE bot_id = (SELECT id FROM bots WHERE name = 'xrp long_hedge')
ORDER BY created_at DESC LIMIT 10;
""").fetchall()
for r in rows2:
    print(f"  cycle={r['cycle_id']} | type={r['order_type']:6s} | status={r['status']:10s} | filled={r['filled_amount']} | cid={r['client_order_id']}")

db.close()
