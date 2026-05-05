from engine.database import get_connection
import pandas as pd
from engine.exchange_interface import normalize_symbol

conn = get_connection()
cursor = conn.cursor()

# This is the query from monitor.py
query_virtual = """
    SELECT b.pair, b.direction,
           t.total_invested, t.avg_entry_price,
           COALESCE(t.open_qty, 0) as open_qty,
           b.status as bot_status
    FROM bots b
    JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
"""
df_v = pd.read_sql(query_virtual, conn)
df_v['pair_norm'] = df_v['pair'].apply(normalize_symbol)

print("--- Virtual Positions by Norm Pair ---")
sui_v = df_v[df_v['pair_norm'] == 'SUIUSDC']
print(sui_v)

# Check Hedges
query_hedge = """
    SELECT pair, direction, SUM(filled_amount) as hedge_qty
    FROM bot_orders
    WHERE order_type IN ('hedge', 'hedge_tp')
    GROUP BY pair, direction
"""
df_h = pd.read_sql(query_hedge, conn)
df_h['pair_norm'] = df_h['pair'].apply(normalize_symbol)
print("\n--- Hedges by Norm Pair ---")
sui_h = df_h[df_h['pair_norm'] == 'SUIUSDC']
print(sui_h)
