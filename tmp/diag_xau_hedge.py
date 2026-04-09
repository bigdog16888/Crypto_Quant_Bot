import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print("=== XAU HEDGE orders ===")
c.execute("SELECT order_type, amount, filled_amount, price, status, created_at FROM bot_orders WHERE bot_id=10019 AND order_type='hedge'")
for r in c.fetchall():
    print(r)

print("\n=== ALL XAU orders with filled>0 ===")
c.execute("""
    SELECT order_type, amount, filled_amount, price, status, created_at, client_order_id
    FROM bot_orders WHERE bot_id=10019 AND filled_amount > 0 ORDER BY created_at DESC LIMIT 20
""")
for r in c.fetchall():
    print(r)
    
print("\n=== XAU active_positions ===")
c.execute("SELECT * FROM active_positions WHERE pair LIKE '%XAU%'")
for r in c.fetchall():
    print(r)

conn.close()
