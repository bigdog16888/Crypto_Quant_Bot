import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()
c.execute("SELECT id FROM bots WHERE pair LIKE '%LINK%'")
bid = c.fetchone()[0]

print(f"Bot {bid} Orders:")
c.execute("SELECT order_type, amount, price, status, created_at, order_id FROM bot_orders WHERE bot_id=? ORDER BY created_at ASC", (bid,))
for r in c.fetchall():
    print(r)
    
conn.close()
