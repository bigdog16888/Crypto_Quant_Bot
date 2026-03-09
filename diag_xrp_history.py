import sys, os
sys.path.append(os.path.abspath('.'))
from engine.database import get_connection

conn = get_connection()
c = conn.cursor()

c.execute("SELECT timestamp, action, symbol, amount, price FROM trade_history WHERE bot_id=10017 ORDER BY timestamp DESC LIMIT 100")
print('XRP Recent Actions:')
for r in c.fetchall():
    if r[0] <= 1773025367 and r[0] > 1773020000:
         print(f"[{r[0]}] {r[1]:<15} {r[3]:>8.2f} @ {r[4]:.4f}")

conn.close()
