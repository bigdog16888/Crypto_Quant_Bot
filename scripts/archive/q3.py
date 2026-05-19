import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

q1 = '''
SELECT bot_id, order_id, order_type, filled_amount, status, created_at
FROM bot_orders
WHERE bot_id IN (10008, 10020)
AND order_type = 'tp'
ORDER BY created_at DESC
LIMIT 20;
'''

q2 = '''
SELECT bot_id, open_qty, total_invested, current_step, cycle_id, tp_order_id
FROM trades
WHERE bot_id IN (10008, 10020);
'''

print("Query 1:")
print(pd.read_sql_query(q1, conn).to_string(index=False))

print("\nQuery 2:")
print(pd.read_sql_query(q2, conn).to_string(index=False))
