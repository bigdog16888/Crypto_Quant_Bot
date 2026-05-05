from engine.database import get_connection
import pandas as pd

conn = get_connection()
query = """
    SELECT id, bot_id, order_type, side, filled_amount, status 
    FROM bot_orders 
    WHERE bot_id IN (10016, 10022) 
      AND (order_type LIKE 'hedge%')
    ORDER BY id DESC;
"""
df = pd.read_sql(query, conn)
print(df)
