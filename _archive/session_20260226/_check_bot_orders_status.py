import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
df = pd.read_sql_query("SELECT bot_id, status, order_type, step, created_at, client_order_id FROM bot_orders WHERE order_type='buy' OR order_type='sell'", conn)
print(df.to_string())
