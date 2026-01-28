import sqlite3
import os

db_path = r"C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
for table in tables:
    print(table[0])
conn.close()
