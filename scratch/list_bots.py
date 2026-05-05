import sqlite3
import pandas as pd

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
conn = sqlite3.connect(db_path)

print("--- ALL BOTS ---")
df_bots = pd.read_sql("SELECT id, name, pair FROM bots", conn)
print(df_bots)

conn.close()
