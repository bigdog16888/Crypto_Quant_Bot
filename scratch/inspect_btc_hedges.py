from engine.database import get_connection
import pandas as pd

conn = get_connection()
query = """
    SELECT o.id, o.bot_id, b.direction, o.order_type, o.filled_amount, o.status, o.created_at
    FROM bot_orders o
    JOIN bots b ON o.bot_id = b.id
    WHERE b.pair LIKE '%BTC%USDC%'
      AND (o.order_type LIKE 'hedge%')
    ORDER BY o.id DESC;
"""
df = pd.read_sql(query, conn)
print(df)
