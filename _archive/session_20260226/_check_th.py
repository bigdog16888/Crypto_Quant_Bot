import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
print('=== BOT 10009 TRADE HISTORY ===')
try:
    df = pd.read_sql('SELECT action, price, profit, datetime(timestamp, "unixepoch", "localtime") as dt FROM trade_history WHERE bot_id=10009 ORDER BY timestamp DESC LIMIT 5', conn)
    print(df)
except Exception as e:
    print(e)
conn.close()
