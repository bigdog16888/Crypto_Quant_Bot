from engine.database import get_connection
import pandas as pd

conn = get_connection()
query = "SELECT * FROM bot_orders WHERE bot_id = 10016 AND status = 'filled' ORDER BY id DESC LIMIT 20;"
print(pd.read_sql(query, conn))
