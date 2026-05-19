import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id IN (10008, 10020)")
conn.commit()

df = pd.read_sql_query('''
SELECT bot_id, open_qty, total_invested, current_step, tp_order_id 
FROM trades WHERE bot_id IN (10008, 10020);
''', conn)
print(df.to_string(index=False))
