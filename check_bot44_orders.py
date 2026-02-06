"""Check Bot 44 orders in database"""
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Bot 44 orders with CQB tags
cur.execute("""
    SELECT order_id, order_type, status, client_order_id, price, amount, created_at
    FROM bot_orders 
    WHERE bot_id = 44 AND client_order_id IS NOT NULL
    ORDER BY id DESC LIMIT 30
""")
cqb_orders = cur.fetchall()
print(f"BOT 44 ORDERS WITH CLIENT_ORDER_ID: {len(cqb_orders)}")
for o in cqb_orders[:15]:
    print(f"  {o[0]} | {o[1]:6} | {o[2]:8} | {o[3]} | ${o[4]} x {o[5]}")

# Check ALL bot_orders for Bot 44
cur.execute("""
    SELECT order_type, status, COUNT(*) 
    FROM bot_orders 
    WHERE bot_id = 44
    GROUP BY order_type, status
""")
summary = cur.fetchall()
print(f"\nBOT 44 ORDER SUMMARY:")
for s in summary:
    print(f"  {s[0]:10} | {s[1]:10} | Count: {s[2]}")

# Check if Bot 44 has any 'sell' orders (would indicate SHORT entry/TP)
cur.execute("""
    SELECT order_id, order_type, price, amount, client_order_id 
    FROM bot_orders 
    WHERE bot_id = 44 
    ORDER BY id DESC LIMIT 50
""")
all_orders = cur.fetchall()
print(f"\nBOT 44 LAST 15 ORDERS:")
for o in all_orders[:15]:
    print(f"  {o[1]:8} | ID: {o[0]:15} | ${o[2]} x {o[3]} | CID: {o[4]}")

conn.close()
