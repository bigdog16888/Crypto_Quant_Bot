import sqlite3

db = sqlite3.connect('crypto_bot.db')

result = db.execute("""
INSERT OR IGNORE INTO bot_orders
  (bot_id, order_type, order_id, client_order_id, price, amount,
   filled_amount, status, step, cycle_id, position_side, created_at, notes)
VALUES
  (100313, 'tp', 'PENDING_BE_100313_63_2', 'CQB_100313_TP_63_BE_2',
   1.3225, 689.7, 0.0, 'pending_placement', 1, 63, 'SHORT',
   strftime('%s','now'), 'Break-even TP re-insert: maintain_orders pickup')
""")
db.commit()
print(f"Rows inserted: {result.rowcount}")

# Verify
rows = db.execute("""
SELECT id, bot_id, order_type, status, price, amount, cycle_id, position_side, notes
FROM bot_orders WHERE bot_id=100313
AND status IN ('open','new','pending_placement')
ORDER BY created_at DESC LIMIT 5
""").fetchall()
print(f"\nVerification — bot_orders for bot 100313:")
if rows:
    for r in rows:
        print(f"  id={r[0]}  type={r[2]}  status={r[3]}  price={r[4]}  amt={r[5]}  cycle={r[6]}  side={r[7]}")
else:
    print("  EMPTY")

db.close()
