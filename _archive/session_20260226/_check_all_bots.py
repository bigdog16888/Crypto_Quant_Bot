import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)
print("=== BOTS 10012 and 10013 ===")
print(pd.read_sql_query("SELECT id, name, pair, direction, is_active, status FROM bots WHERE id IN (10012, 10013)", conn))
print("\n=== TRADES for 10012 and 10013 ===")
print(pd.read_sql_query("SELECT bot_id, total_invested, avg_entry_price, current_step, entry_confirmed FROM trades WHERE bot_id IN (10012, 10013)", conn))
conn.close()
