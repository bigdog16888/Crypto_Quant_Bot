import sqlite3, os

db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_bot.db")
conn = sqlite3.connect(db_path)
cur = conn.cursor()
print("DB:", db_path)
cur.execute("PRAGMA table_info(bots)")
print("== bots columns ==")
for r in cur.fetchall():
    print(r)
cur.execute("PRAGMA table_info(bot_orders)")
print("== bot_orders columns ==")
for r in cur.fetchall():
    print(r)
cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='bot_orders'")
print("== bot_orders indexes ==")
for r in cur.fetchall():
    print(r)
cur.execute("SELECT id FROM bots WHERE id=100317")
print("bot 100317 exists:", cur.fetchone())
cur.execute("SELECT id FROM bots WHERE id=200000")
print("bot 200000 exists:", cur.fetchone())
conn.close()
