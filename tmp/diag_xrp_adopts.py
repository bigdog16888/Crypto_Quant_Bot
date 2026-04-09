import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
df = pd.read_sql_query("SELECT cycle_id, order_id, status, amount, filled_amount, created_at FROM bot_orders WHERE order_type='adoption' AND bot_id=10017", conn)
print("=== All Adoptions for XRP bot (10017) ===")
print(df)
conn.close()
