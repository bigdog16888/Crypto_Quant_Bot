from engine.database import get_connection
import pandas as pd

conn = get_connection()
query = "SELECT * FROM bot_orders WHERE order_type='hedge' AND bot_id IN (SELECT id FROM bots WHERE pair LIKE '%LINK%');"
print(pd.read_sql(query, conn))
