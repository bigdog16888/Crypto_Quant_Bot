import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')
query = """
SELECT o.status, o.order_type, SUM(o.filled_amount) as total_filled, SUM(o.amount) as total_size 
FROM bot_orders o
JOIN bots b ON o.bot_id = b.id 
WHERE b.pair='SUI/USDC:USDC' 
GROUP BY o.status, o.order_type
"""
df = pd.read_sql(query, conn)
print(df.to_string())
