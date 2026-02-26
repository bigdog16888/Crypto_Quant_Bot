import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 2000)

print('=== TRADES TABLE (ETH/USDC) ===')
try:
    df_trades = pd.read_sql('SELECT bot_id, total_invested, avg_entry_price, current_step, entry_order_id, tp_order_id FROM trades WHERE bot_id IN (10010, 10013)', conn)
    print(df_trades)
except Exception as e:
    print(e)

print('\n=== FILLED ORDERS (ALL ETH/USDC) SINCE 16:13 ===')
try:
    df_orders = pd.read_sql('''
        SELECT bot_id, order_type, step, price, amount, amount * price as notional, status, datetime(created_at, "unixepoch", "localtime") as created 
        FROM bot_orders 
        WHERE bot_id IN (10010, 10013) AND created_at >= 1771834394
        ORDER BY created_at ASC
    ''', conn)
    print(df_orders)
    print(f"\nTotal Filled Notional: {df_orders[df_orders['status'] == 'filled']['notional'].sum()}")
except Exception as e:
    print(e)

print('\n=== RECENT TRADE HISTORY (ETH/USDC) ===')
try:
    df_th = pd.read_sql('''
        SELECT bot_id, action, pnl, datetime(timestamp, "unixepoch", "localtime") as dt 
        FROM trade_history 
        WHERE bot_id IN (10010, 10013) AND timestamp >= 1771834394
        ORDER BY timestamp ASC
    ''', conn)
    print(df_th)
except Exception as e:
    print(e)

conn.close()
