from engine.database import get_connection
import pandas as pd

conn = get_connection()
query_bots = "SELECT id, pair, direction, is_active, status FROM bots WHERE pair LIKE '%SUI%'"
df_bots = pd.read_sql(query_bots, conn)
print("--- SUI Bots ---")
print(df_bots)

if not df_bots.empty:
    bot_ids = tuple(df_bots['id'].tolist())
    if len(bot_ids) == 1:
        query_trades = f"SELECT bot_id, open_qty, total_invested FROM trades WHERE bot_id = {bot_ids[0]}"
    else:
        query_trades = f"SELECT bot_id, open_qty, total_invested FROM trades WHERE bot_id IN {bot_ids}"
    df_trades = pd.read_sql(query_trades, conn)
    print("\n--- SUI Trades ---")
    print(df_trades)
