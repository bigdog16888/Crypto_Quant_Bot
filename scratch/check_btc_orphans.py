from engine.database import get_connection
import pandas as pd

conn = get_connection()
order_ids = ['55313809', '55637397', '55692069', '55735216', '56028527', '56030194', '57290877']
query = f"SELECT * FROM bot_orders WHERE order_id IN ({','.join(['?' for _ in order_ids])})"
print(pd.read_sql(query, conn, params=order_ids))
