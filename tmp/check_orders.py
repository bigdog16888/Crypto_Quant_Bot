import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== TRADES ===")
q.execute("SELECT bot_id, cycle_id, total_invested, avg_entry_price FROM trades WHERE bot_id IN (10016, 10017, 10008, 10019)")
for row in q.fetchall():
    print(row)

print("\n=== FILLED BOT_ORDERS (non-reset) ===")
q.execute("""
    SELECT bot_id, order_type, status, filled_amount, cycle_id 
    FROM bot_orders 
    WHERE bot_id IN (10016, 10017, 10008, 10019) 
    AND status NOT IN ('reset_cleared', 'auto_closed') 
    AND filled_amount > 0 
    ORDER BY bot_id, created_at DESC 
    LIMIT 30
""")
for row in q.fetchall():
    print(row)

print("\n=== ALL STATUS counts per bot ===")
q.execute("""
    SELECT bot_id, status, COUNT(*) as cnt, SUM(filled_amount) as total_filled 
    FROM bot_orders 
    WHERE bot_id IN (10016, 10017, 10008, 10019)
    GROUP BY bot_id, status
    ORDER BY bot_id, status
""")
for row in q.fetchall():
    print(row)

c.close()
