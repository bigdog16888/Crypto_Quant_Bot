import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
print('=== BOT 10009 TRADE_HISTORY AFTER 13:00 ===')
try:
    df = pd.read_sql('SELECT action, price, pnl, datetime(timestamp, "unixepoch", "localtime") as dt FROM trade_history WHERE bot_id=10009 AND timestamp > 1771822800 ORDER BY timestamp ASC', conn)
    print(df)
except Exception as e:
    print(e)
conn.close()
