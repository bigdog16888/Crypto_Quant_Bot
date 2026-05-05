import sqlite3
import pandas as pd
import os

db_path = r'c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
conn = sqlite3.connect(db_path)

bot_id = 10022
cycle_id = 11

print(f"--- BOT {bot_id} (short btc) CYCLE {cycle_id} ORDERS ---")
df_orders = pd.read_sql(f"""
    SELECT id, order_type, price, amount, filled_amount, status, position_side, client_order_id
    FROM bot_orders 
    WHERE bot_id={bot_id} AND cycle_id={cycle_id}
""", conn)
print(df_orders)

print("\n--- TRADES TABLE SIDE ---")
df_trades = pd.read_sql(f"SELECT position_side FROM trades WHERE bot_id={bot_id}", conn)
print(df_trades)

conn.close()
