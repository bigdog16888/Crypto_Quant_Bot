from engine.database import get_connection
import pandas as pd

conn = get_connection()
query = "SELECT id, name, pair, direction, is_active FROM bots WHERE pair LIKE '%BTC%USDC%';"
df = pd.read_sql(query, conn)
print(df)

print("\n--- Active Trades for these bots ---")
ids = tuple(df['id'].tolist())
if ids:
    query_trades = f"SELECT bot_id, open_qty, total_invested FROM trades WHERE bot_id IN {ids};"
    print(pd.read_sql(query_trades, conn))
