import sqlite3

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

bot_id = 10019
print(f"--- BOT ORDERS FOR {bot_id} ---")
cursor.execute("SELECT id, step, order_type, order_id, status, filled_amount, cycle_id, created_at FROM bot_orders WHERE bot_id=? ORDER BY created_at DESC LIMIT 20", (bot_id,))
for row in cursor.fetchall():
    print(row)

conn.close()
