from engine.database import get_connection
import pandas as pd

conn = get_connection()
order_ids = ['337571851', '337571993', '337571154', '337570388', '337570384']
query = f"SELECT * FROM bot_orders WHERE order_id IN ({','.join(['?' for _ in order_ids])})"
print(pd.read_sql(query, conn, params=order_ids))
