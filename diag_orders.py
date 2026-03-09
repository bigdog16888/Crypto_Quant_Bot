import sys, os, json
sys.path.append(os.path.abspath('.'))
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

ex = ExchangeInterface()
all_orders = ex.safe_fetch_open_orders()
print(f"Total Exchange Open Orders: {len(all_orders)}")

conn = get_connection()
c = conn.cursor()
c.execute("SELECT id, name FROM bots")
bots = c.fetchall()
bot_names = {r[0]: r[1] for r in bots}

c.execute("SELECT order_type, order_id, status FROM bot_orders WHERE status='open'")
db_orders = {r[1]: r[0] for r in c.fetchall()}

for o in all_orders:
    oid = o['id']
    cid = o.get('clientOrderId', '')
    symbol = o.get('symbol', 'UNKNOWN')
    side = o.get('side', 'UNKNOWN')
    price = o.get('price', 0)
    qty = o.get('amount', 0)
    
    bot_tag = ""
    if cid.startswith("CQB_"):
        bot_id = int(cid.split("_")[1])
        bot_tag = f"Bot {bot_id} ({bot_names.get(bot_id, 'Unknown')})"
    
    db_status = "IN DB" if oid in db_orders else "stray"
    print(f"[{side:5}] {symbol:<15} {qty:>8.3f} @ {price:>8.3f} | {bot_tag:<30} | {db_status}")

c.execute('SELECT b.id, b.name, t.total_invested, t.avg_entry_price FROM bots b JOIN trades t ON b.id=t.bot_id')
for r in c.fetchall():
    pass
conn.close()
