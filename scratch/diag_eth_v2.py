import sqlite3
import os

db_path = "crypto_bot.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("--- All Pairs ---")
rows = cur.execute("SELECT DISTINCT pair FROM bots;").fetchall()
for r in rows:
    print(r)

print("\n--- Recent Fills (Last 20) ---")
f_rows = cur.execute("SELECT b.name, b.pair, bo.client_order_id, bo.filled_amount, bo.price, datetime(bo.created_at, 'unixepoch', 'localtime') FROM bot_orders bo JOIN bots b ON bo.bot_id = b.id WHERE bo.status = 'filled' ORDER BY bo.created_at DESC LIMIT 20;").fetchall()
for fr in f_rows:
    print(fr)

conn.close()
