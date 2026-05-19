import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

print("Query 1:")
df1 = pd.read_sql_query('''
SELECT bot_id, order_type, filled_amount, price, status, cycle_id
FROM bot_orders
WHERE bot_id IN (
    SELECT id FROM bots WHERE pair LIKE '%LINK%' AND is_active = 1
)
AND status NOT IN ('reset_cleared','cancelled','canceled','failed','auto_closed')
AND filled_amount > 0
ORDER BY bot_id, created_at DESC;
''', conn)
print(df1.to_string(index=False))

print("\nQuery 2:")
df2 = pd.read_sql_query('''
SELECT b.id, b.name, b.direction, t.open_qty, t.total_invested, t.cycle_id
FROM bots b JOIN trades t ON t.bot_id = b.id
WHERE b.pair LIKE '%LINK%' AND b.is_active = 1;
''', conn)
print(df2.to_string(index=False))
