import sqlite3

db_path = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("--- TRADE HISTORY: XRP ---")
cursor.execute("SELECT timestamp, action, symbol, price, amount, cost_usdc, step, pnl, notes FROM trade_history WHERE symbol LIKE '%XRP%' ORDER BY id DESC LIMIT 15")
for r in cursor.fetchall():
    print(r)

print("--- BOT ORDERS: XRP ---")
cursor.execute("SELECT bot_id, step, order_type, side, status, amount, price, created_at, updated_at FROM bot_orders WHERE bot_id=10010 ORDER BY id DESC LIMIT 15")
for r in cursor.fetchall():
    print(r)

conn.close()
