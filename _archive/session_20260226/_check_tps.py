import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
print('=== ALL ORDERS ===')
try:
    df = pd.read_sql('SELECT bot_id, status, order_type, price, amount, datetime(updated_at, "unixepoch", "localtime") as dt FROM bot_orders WHERE status IN ("filled", "closed", "auto_closed") ORDER BY updated_at DESC', conn)
    print(df)
except Exception as e:
    print(e)
conn.close()
