import sqlite3
import os

db_path = 'crypto_bot.db'
if not os.path.exists(db_path):
    print("DB not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- BOT 10022 HEDGE TP ORDERS ---")
rows = cursor.execute("SELECT id, order_type, step, filled_amount, status, client_order_id, cycle_id, order_id FROM bot_orders WHERE bot_id=10022 AND order_type='hedge_tp' ORDER BY id DESC").fetchall()
for r in rows:
    print(r)

print("\n--- BOT 10022 CARRY ORDERS ---")
rows = cursor.execute("SELECT id, order_type, step, filled_amount, status, client_order_id, cycle_id, order_id FROM bot_orders WHERE bot_id=10022 AND client_order_id LIKE '%CARRY%' ORDER BY id DESC").fetchall()
for r in rows:
    print(r)

print("\n--- BOT 10022 TRADES ROW ---")
row = cursor.execute("SELECT current_step, total_invested, avg_entry_price, cycle_phase FROM trades WHERE bot_id=10022").fetchone()
print(row)

conn.close()
