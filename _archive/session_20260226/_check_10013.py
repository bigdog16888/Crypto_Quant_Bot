import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== BOT 10013 ORDERS ===')
try:
    df_orders = pd.read_sql('SELECT bot_id, status, order_type, price, amount, datetime(updated_at, "unixepoch", "localtime") as updated, datetime(created_at, "unixepoch", "localtime") as created FROM bot_orders WHERE bot_id=10013 ORDER BY created_at DESC', conn)
    print(df_orders)
except Exception as e:
    print(e)
conn.close()
