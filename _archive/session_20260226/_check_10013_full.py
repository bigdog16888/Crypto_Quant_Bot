import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== FULL TRADE HISTORY FOR BOT 10013 (SINCE WIPE) ===')
try:
    # 16:13 is 1771834394
    df_th = pd.read_sql('''
        SELECT id, action, price, amount, pnl, datetime(timestamp, "unixepoch", "localtime") as dt, notes 
        FROM trade_history 
        WHERE bot_id = 10013 AND timestamp >= 1771834394
        ORDER BY timestamp ASC
    ''', conn)
    print(df_th)
except Exception as e:
    print(e)

print('\n=== ALL ORDERS FOR BOT 10013 (SINCE WIPE) ===')
try:
    df_orders = pd.read_sql('''
        SELECT order_id, order_type, status, price, amount, datetime(created_at, "unixepoch", "localtime") as created, notes
        FROM bot_orders 
        WHERE bot_id = 10013 AND created_at >= 1771834394
        ORDER BY created_at ASC
    ''', conn)
    print(df_orders)
except Exception as e:
    print(e)

conn.close()
