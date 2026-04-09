import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print("--- SUI (Bot 10018) ---")
c.execute("""
    SELECT step, order_type, price, amount, filled_amount, status, 
           datetime(created_at,'unixepoch','localtime') 
    FROM bot_orders 
    WHERE bot_id=10018 AND order_type IN ('tp', 'entry', 'grid')
    ORDER BY created_at DESC LIMIT 15
""")
for r in c.fetchall():
    print(r)

print("\n--- LINK (Bot 10020) ---")
c.execute("""
    SELECT step, order_type, price, amount, filled_amount, status, 
           datetime(created_at,'unixepoch','localtime') 
    FROM bot_orders 
    WHERE bot_id=10020 AND order_type IN ('tp', 'entry', 'grid')
    ORDER BY created_at DESC LIMIT 15
""")
for r in c.fetchall():
    print(r)

conn.close()
