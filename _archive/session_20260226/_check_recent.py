import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== RECENT TRADE HISTORY ===')
try:
    df1 = pd.read_sql('SELECT bot_id, action, pnl, datetime(timestamp, "unixepoch", "localtime") as dt FROM trade_history ORDER BY timestamp DESC LIMIT 15', conn)
    print(df1)
except Exception as e:
    print(e)

print('\n=== RECENT BOT ORDERS ===')
try:
    df2 = pd.read_sql('SELECT bot_id, status, order_type, amount, datetime(created_at, "unixepoch", "localtime") as dt FROM bot_orders ORDER BY created_at DESC LIMIT 15', conn)
    print(df2)
except Exception as e:
    print(e)
conn.close()
