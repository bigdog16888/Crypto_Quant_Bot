import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
query = """
SELECT bot_id, status, SUM(amount * price) as vol, MAX(created_at) as last_fill 
FROM bot_orders 
WHERE bot_id IN (10012, 10013) AND status = 'filled' 
GROUP BY bot_id
"""
print(pd.read_sql_query(query, conn))
conn.close()
