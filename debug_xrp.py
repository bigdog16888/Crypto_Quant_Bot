import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
b = pd.read_sql("SELECT id, name, pair FROM bots WHERE name LIKE '%xrp%'", conn)
print('--- Bots ---')
print(b)
for bot_id in b['id'].tolist():
    print(f"\n--- Bot {bot_id} Orders ---")
    orders = pd.read_sql(f"SELECT order_type, price, amount, status FROM bot_orders WHERE bot_id={bot_id} AND status != 'closed'", conn)
    print(orders)
    
    print(f"\n--- Trades ---")
    trades = pd.read_sql(f"SELECT current_step, total_invested, avg_entry_price FROM trades WHERE bot_id={bot_id}", conn)
    print(trades)
    
conn.close()
