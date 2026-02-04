import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
query = "SELECT * FROM trade_history WHERE bot_id=37 ORDER BY timestamp DESC LIMIT 20"
df = pd.read_sql_query(query, conn)
print(df)
conn.close()
