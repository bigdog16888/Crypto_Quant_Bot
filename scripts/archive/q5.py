import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

q1 = '''
SELECT bot_id, order_type, filled_amount, price, status, cycle_id, created_at
FROM bot_orders
WHERE bot_id = 10020
AND status NOT IN ('reset_cleared','cancelled','canceled','failed')
AND filled_amount > 0
ORDER BY created_at DESC
LIMIT 20;
'''

q2 = '''
SELECT bot_id, open_qty, total_invested, current_step, 
       cycle_id, tp_order_id, avg_entry_price, position_side
FROM trades WHERE bot_id = 10020;
'''

print("Query 1:")
print(pd.read_sql_query(q1, conn).to_string(index=False))

print("\nQuery 2:")
print(pd.read_sql_query(q2, conn).to_string(index=False))
