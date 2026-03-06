import sqlite3
import os

db_path = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\engine\trades.db'
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- BOTS TABLE ---")
cursor.execute("SELECT id, name, pair, direction, status FROM bots WHERE id=10011 OR id=10013 OR id=8")
for row in cursor.fetchall():
    print(row)

print("\n--- TRADES TABLE ---")
cursor.execute("SELECT bot_id, current_step, total_invested, avg_entry_price, target_tp_price, basket_start_time FROM trades WHERE bot_id=10011 OR bot_id=10013 OR bot_id=8")
for row in cursor.fetchall():
    print(row)

conn.close()
