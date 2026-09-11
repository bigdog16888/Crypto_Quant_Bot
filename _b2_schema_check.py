import sqlite3, os
db = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot_INV31\crypto_bot.db"
print("exists:", os.path.exists(db), "size:", os.path.getsize(db) if os.path.exists(db) else None)
conn = sqlite3.connect(db)
print("--- bot_orders columns ---")
for r in conn.execute("PRAGMA table_info(bot_orders)"):
    print(r)
print("--- bots columns ---")
for r in conn.execute("PRAGMA table_info(bots)"):
    print(r)
print("--- indexes on bot_orders ---")
for r in conn.execute("PRAGMA index_list(bot_orders)"):
    print(r)
    for c in conn.execute(f"PRAGMA index_info({r[1]})"):
        print("   ", c)
print("--- existing bot_orders rows for bot 100317 ---")
for r in conn.execute("SELECT id, bot_id, client_order_id, status FROM bot_orders WHERE bot_id=100317 LIMIT 10"):
    print(r)
print("--- existing bots id=200000 ---")
for r in conn.execute("SELECT id, pair, status FROM bots WHERE id=200000"):
    print(r)
