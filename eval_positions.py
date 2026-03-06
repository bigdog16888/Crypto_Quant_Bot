import sqlite3
import pandas as pd

conn = sqlite3.connect('crypto_bot.db')

# Check open orders per bot
query = """
SELECT bo.bot_id, b.name, b.pair, b.direction, bo.order_type, bo.order_id, bo.status
FROM bot_orders bo
JOIN bots b ON bo.bot_id = b.id
WHERE bo.status = 'open'
ORDER BY bo.bot_id
"""
try:
    df = pd.read_sql_query(query, conn)
    print("--- OPEN ORDERS PER BOT ---")
    print(df.to_string())
except Exception as e:
    print("Error:", e)

# Also show trades summary
query2 = """
SELECT b.id, b.name, b.pair, b.direction, b.status, t.total_invested, t.current_step, t.entry_order_id, t.tp_order_id
FROM bots b
LEFT JOIN trades t ON b.id = t.bot_id
WHERE b.status IN ('IN TRADE', 'Scanning')
ORDER BY b.pair, b.direction
"""
try:
    df2 = pd.read_sql_query(query2, conn)
    print("\n--- ACTIVE BOTS TRADES ---")
    print(df2.to_string())
except Exception as e:
    print("Error:", e)

conn.close()
