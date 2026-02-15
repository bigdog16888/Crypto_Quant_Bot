import sqlite3
import os

db_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("PRAGMA table_info(bots)")
columns = cursor.fetchall()
for col in columns:
    print(col)
conn.close()
