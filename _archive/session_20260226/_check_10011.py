import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
print('=== BOT 10011 TRADE HISTORY ===')
try:
    df = pd.read_sql('SELECT action, pnl, datetime(timestamp, "unixepoch", "localtime") as dt FROM trade_history WHERE bot_id=10011', conn)
    print(df)
except Exception as e:
    print(e)
    
print('=== ALL TP/SL CANCELS ===')
try:
    df2 = pd.read_sql('SELECT bot_id, status, order_type, datetime(updated_at, "unixepoch", "localtime") as dt FROM bot_orders WHERE order_type="tp" AND status="cancelled" AND updated_at > 1771830000', conn)
    print(df2)
except Exception as e:
    print(e)
conn.close()
