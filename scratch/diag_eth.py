import sqlite3
import os

db_path = "crypto_bot.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("--- ETHUSDC Bots ---")
rows = cur.execute("SELECT id, name, config, status FROM bots WHERE pair LIKE 'ETHUSDC%';").fetchall()
for r in rows:
    print(r)

print("\n--- Recent ETHUSDC Fills ---")
# Check last 10 fills for ETHUSDC bots
bot_ids = [r[0] for r in rows]
if bot_ids:
    placeholders = ','.join(['?'] * len(bot_ids))
    f_rows = cur.execute(f"SELECT bot_id, client_order_id, filled_amount, price, created_at FROM bot_orders WHERE bot_id IN ({placeholders}) AND status = 'filled' ORDER BY created_at DESC LIMIT 10;").fetchall()
    for fr in f_rows:
        print(fr)

conn.close()
