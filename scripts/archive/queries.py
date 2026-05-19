import sqlite3, pandas as pd
conn = sqlite3.connect('crypto_bot.db')

# Rollback the blind injection
conn.execute("DELETE FROM bot_orders WHERE bot_id = 10015 AND order_type = 'forensic_adoption' AND filled_amount = 0.003 AND cycle_id = 3")
conn.commit()

df = pd.read_sql_query('''
SELECT b.id, b.name, b.direction, t.cycle_id, t.total_invested, 
       t.open_qty, t.current_step, t.wipe_wall_ts
FROM bots b JOIN trades t ON t.bot_id = b.id
WHERE b.pair LIKE '%BTC%' AND b.is_active = 1;
''', conn)
print('Query 1:')
print(df.to_string(index=False))

df2 = pd.read_sql_query('''
SELECT bot_id, order_type, filled_amount, price, status, 
       cycle_id, client_order_id, created_at
FROM bot_orders
WHERE bot_id IN (10015, 10016, 10022)
AND filled_amount > 0
AND status NOT IN ('reset_cleared','cancelled','canceled','failed')
ORDER BY created_at DESC LIMIT 15;
''', conn)
print('\nQuery 2:')
print(df2.to_string(index=False))
