import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
print('=== RECENT TRADES ===')
try:
    df1 = pd.read_sql('SELECT bot_id, status, amount, price, datetime(created_at, "unixepoch", "localtime") as dt, client_order_id FROM bot_orders ORDER BY created_at DESC LIMIT 20', conn)
    print(df1)
except Exception as e:
    print(e)

print('\n=== BOT 10009 TRADES ===')
try:
    df2 = pd.read_sql('SELECT status, amount, price, datetime(created_at, "unixepoch", "localtime") as dt, client_order_id FROM bot_orders WHERE bot_id=10009 ORDER BY created_at DESC LIMIT 10', conn)
    print(df2)
except Exception as e:
    print(e)
    
conn.close()
