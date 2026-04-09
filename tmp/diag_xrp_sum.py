import sqlite3
import pandas as pd
conn = sqlite3.connect('crypto_bot.db')
df = pd.read_sql_query("""
    SELECT cycle_id, order_type, ROUND(SUM(filled_amount), 4) as total_filled 
    FROM bot_orders 
    WHERE bot_id=10017 AND status IN ('filled', 'closed') AND order_type IN ('entry', 'grid')
    GROUP BY cycle_id
""", conn)
print("=== Filled Entry/Grid grouped by cycle ===")
print(df)
conn.close()
