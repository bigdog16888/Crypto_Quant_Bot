import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

c.execute("SELECT order_type, amount, price, status, created_at FROM bot_orders WHERE bot_id=10017 ORDER BY created_at DESC LIMIT 50")
for r in c.fetchall():
    print(r)

conn.close()
