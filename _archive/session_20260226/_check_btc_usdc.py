import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== ALL BTC/USDC ORDERS ===')
try:
    df_orders = pd.read_sql('SELECT bot_id, status, order_type, price, amount, datetime(created_at, "unixepoch", "localtime") as dt FROM bot_orders WHERE status="open" OR status="filled" ORDER BY created_at DESC LIMIT 20', conn)
    print(df_orders)
except Exception as e:
    print(e)
    
conn.close()
