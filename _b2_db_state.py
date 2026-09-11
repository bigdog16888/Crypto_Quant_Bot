import sqlite3, os
db = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot_INV31\crypto_bot.db"
conn = sqlite3.connect(db)
print("bots id=100317:", conn.execute("SELECT id,name,pair,status FROM bots WHERE id=100317").fetchall())
print("trades bot 100317:", conn.execute("SELECT bot_id,open_qty,cycle_id FROM trades WHERE bot_id=100317").fetchall())
print("total bots:", conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0])
print("total trades:", conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
print("total bot_orders:", conn.execute("SELECT COUNT(*) FROM bot_orders").fetchone()[0])
print("bots sample:", conn.execute("SELECT id,name,status FROM bots LIMIT 5").fetchall())
print("journal_mode:", conn.execute("PRAGMA journal_mode").fetchone()[0])
conn.close()
