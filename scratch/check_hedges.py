import sqlite3
import os

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

query = "SELECT name FROM sqlite_master WHERE type='table';"
cur.execute(query)
rows = cur.fetchall()
for row in rows:
    print(row[0])

conn.close()
