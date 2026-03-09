import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

c.execute("SELECT timestamp, action, symbol, amount, price, pnl FROM trade_history WHERE bot_id=10017 AND action IN ('TP_HIT', 'TP') ORDER BY timestamp DESC LIMIT 3")
print("XRP PNL:")
for r in c.fetchall():
    print(r)

conn.close()
