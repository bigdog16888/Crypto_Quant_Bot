import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
print(pd.read_sql_query("SELECT bot_id, order_type, price, amount, status, client_order_id FROM bot_orders WHERE order_type='adoption'", conn))
conn.close()
