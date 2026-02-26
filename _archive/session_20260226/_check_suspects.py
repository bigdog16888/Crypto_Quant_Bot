import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== ALL ORDERS RECENT ===')
try:
    df_orders = pd.read_sql('SELECT bot_id, status, order_type, price, amount, datetime(created_at, "unixepoch", "localtime") as dt FROM bot_orders WHERE status="filled" AND created_at > 1771830000', conn)
    print(df_orders)
except Exception as e:
    print(e)
    
conn.close()
