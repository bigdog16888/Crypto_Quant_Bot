import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== ACTIVE TRADES ===')
try:
    df_trades = pd.read_sql('SELECT bot_id, total_invested, avg_entry_price, current_step, datetime(basket_start_time, "unixepoch", "localtime") as start_time FROM trades WHERE total_invested > 0', conn)
    print(df_trades)
except Exception as e:
    print(e)

print('\n=== ETH/USDC BOTS (10010, 10013) TRADE HISTORY ===')
try:
    df_hist = pd.read_sql('SELECT bot_id, action, price, amount, pnl, datetime(timestamp, "unixepoch", "localtime") as dt, notes FROM trade_history WHERE bot_id IN (10010, 10013) ORDER BY timestamp DESC LIMIT 20', conn)
    print(df_hist)
except Exception as e:
    print(e)

print('\n=== RECENT ORDERS (ALL) ===')
try:
    df_orders = pd.read_sql('SELECT bot_id, status, order_type, price, amount, datetime(updated_at, "unixepoch", "localtime") as updated, datetime(created_at, "unixepoch", "localtime") as created FROM bot_orders WHERE status IN ("filled", "closed") ORDER BY updated_at DESC LIMIT 20', conn)
    print(df_orders)
except Exception as e:
    print(e)

conn.close()
