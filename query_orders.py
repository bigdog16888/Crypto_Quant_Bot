import sqlite3
import pandas as pd

# Connect to database
db = sqlite3.connect('crypto_bot.db')
db.row_factory = sqlite3.Row

# Query 1: All orders for bot_id = 100318
print("=== QUERY 1: All bot_orders for bot 100318 ===")
query1 = """
SELECT order_type, status, filled_amount, amount, price, cycle_id, created_at, client_order_id
FROM bot_orders
WHERE bot_id = 100318
ORDER BY created_at ASC;
"""
df1 = pd.read_sql_query(query1, db)
pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)
print(df1)
print(f"Total rows in Query 1: {len(df1)}")

print("\n" + "="*80 + "\n")

# Query 2: Filled orders for bot_id = 100318 (filled_amount > 0)
print("=== QUERY 2: bot_orders with filled_amount > 0 for bot 100318 ===")
query2 = """
SELECT order_type, status, filled_amount, amount, price, cycle_id, created_at, client_order_id  
FROM bot_orders
WHERE bot_id = 100318
AND filled_amount > 0
ORDER BY created_at ASC;
"""
df2 = pd.read_sql_query(query2, db)
print(df2)
print(f"Total rows in Query 2: {len(df2)}")

db.close()
