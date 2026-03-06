import sqlite3
import os

db_path = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
if not os.path.exists(db_path):
    print("DB NOT FOUND")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- BOTS ---")
cursor.execute("SELECT id, name, pair, direction, config FROM bots WHERE id=10010")
for row in cursor.fetchall():
    print(row)

print("\n--- BOT ORDERS ---")
cursor.execute("SELECT order_id, bot_id, order_type, side, amount, price, status FROM bot_orders WHERE bot_id=10010")
for row in cursor.fetchall():
    print(row)

