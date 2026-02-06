"""Deep investigation - check ALL trade logs and order placement"""
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Check ALL trade_history for Bot 44 including non-BUY/SELL
cur.execute("""
    SELECT action, COUNT(*), MAX(timestamp) 
    FROM trade_history 
    WHERE bot_id = 44
    GROUP BY action
    ORDER BY COUNT(*) DESC
""")
actions = cur.fetchall()
print("BOT 44 - ALL ACTIONS:")
for a in actions:
    print(f"  {a[0]}: {a[1]} times (last: {a[2]})")

# Check if there are ANY SELL records anywhere for Bot 44
cur.execute("""
    SELECT action, symbol, price, amount, notes, timestamp
    FROM trade_history 
    WHERE bot_id = 44 AND action LIKE '%SELL%'
    ORDER BY id DESC LIMIT 20
""")
sells = cur.fetchall()
print(f"\nBOT 44 SELL RECORDS: {len(sells)}")
for s in sells:
    print(f"  {s}")

# Check trades table for Bot 44 - what does it think the state is?
cur.execute("""
    SELECT bot_id, current_step, total_invested, avg_entry_price, entry_order_id, tp_order_id
    FROM trades WHERE bot_id = 44
""")
trade = cur.fetchone()
print(f"\nBOT 44 TRADE STATE:")
print(f"  Step: {trade[1]}")
print(f"  Invested: ${trade[2]}")
print(f"  Avg Entry: ${trade[3]}")
print(f"  Entry Order ID: {trade[4]}")
print(f"  TP Order ID: {trade[5]}")

# Check if Bot 44 ever had total_invested > 0 (meaning it was in trade)
cur.execute("""
    SELECT action, notes, timestamp
    FROM trade_history 
    WHERE bot_id = 44 AND (action = 'TP_HIT' OR notes LIKE '%reset%' OR action LIKE '%CLOSE%')
    ORDER BY id DESC LIMIT 10
""")
closes = cur.fetchall()
print(f"\nBOT 44 CLOSE/RESET EVENTS:")
for c in closes:
    print(f"  {c}")

# Most importantly - check the ACTUAL bot_orders placed
cur.execute("""
    SELECT order_type, COUNT(*), MAX(created_at)
    FROM bot_orders WHERE bot_id = 44
    GROUP BY order_type
""")
order_types = cur.fetchall()
print(f"\nBOT 44 ORDER TYPES PLACED:")
for o in order_types:
    print(f"  {o[0]}: {o[1]} (last: {o[2]})")

conn.close()
