import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

print('=== TRADES TABLE (ETH/USDC) ===')
try:
    df_trades = pd.read_sql('SELECT bot_id, total_invested, current_step, entry_order_id, tp_order_id FROM trades WHERE bot_id IN (10010, 10013)', conn)
    print(df_trades)
except Exception as e:
    print(e)

print('\n=== FILLED ORDERS (ETH/USDC) SINCE 16:13 ===')
# 16:13 is roughly 1771834394
try:
    df_orders = pd.read_sql('''
        SELECT bot_id, order_type, step, price, amount, amount * price as notional, datetime(created_at, "unixepoch", "localtime") as created 
        FROM bot_orders 
        WHERE bot_id IN (10010, 10013) AND status = "filled" AND created_at >= 1771834394
        ORDER BY created_at ASC
    ''', conn)
    print(df_orders)
    print(f"\nTotal Filled Notional: {df_orders['notional'].sum()}")
except Exception as e:
    print(e)

print('\n=== RECENT RECONCILIATION LOGS ===')
try:
    df_recon = pd.read_sql('SELECT * FROM reconciliation_logs ORDER BY timestamp DESC LIMIT 10', conn)
    print(df_recon)
except Exception as e:
    print(e)

conn.close()
