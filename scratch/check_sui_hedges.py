from engine.database import get_connection
import pandas as pd

conn = get_connection()
query_hedge = """
    SELECT bo.id, bo.bot_id, b.pair, b.direction, bo.order_type, bo.filled_amount, bo.status
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE b.pair LIKE '%SUI%'
      AND bo.order_type IN ('hedge', 'hedge_tp')
"""
df_h = pd.read_sql(query_hedge, conn)
print("--- SUI Hedges ---")
print(df_h)
