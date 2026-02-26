import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('\n=== TRADE HISTORY ===')
try:
    df_hist = pd.read_sql('SELECT action, price, amount, pnl, datetime(timestamp, "unixepoch", "localtime") as dt, notes FROM trade_history WHERE bot_id=10015', conn)
    print(df_hist)
except Exception as e:
    print(e)
    
print('\n=== BOT ORDERS ===')
try:
    df_orders = pd.read_sql('SELECT status, order_type, price, amount, datetime(created_at, "unixepoch", "localtime") as dt FROM bot_orders WHERE bot_id=10015', conn)
    print(df_orders)
except Exception as e:
    print(e)

print('\n=== TRADES RECORD ===')
try:
    df_trades = pd.read_sql('SELECT current_step, total_invested, avg_entry_price, entry_order_id, tp_order_id FROM trades WHERE bot_id=10015', conn)
    print(df_trades)
except Exception as e:
    print(e)
    
conn.close()
