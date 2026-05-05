import sqlite3, datetime

conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

def ts(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime('%H:%M:%S') if epoch else 'N/A'

print("=== XAUUSDT (10019) DEEP DIVE ===")
c.execute("SELECT order_type, amount, filled_amount, status, created_at, notes FROM bot_orders WHERE bot_id=10019 AND created_at > ? ORDER BY created_at ASC", (int(datetime.datetime.now().timestamp()) - 7200,))
print("All orders in last 2 hours:")
for o in c.fetchall():
    print(f"  [{ts(o['created_at'])}] type={o['order_type']} amt={o['amount']} fill={o['filled_amount']} status={o['status']}")

print("\n=== BTC SHORT (10022) DEEP DIVE ===")
c.execute("SELECT order_type, amount, filled_amount, status, created_at, notes FROM bot_orders WHERE bot_id=10022 AND created_at > ? ORDER BY created_at ASC", (int(datetime.datetime.now().timestamp()) - 7200,))
print("All orders in last 2 hours:")
for o in c.fetchall():
    if o['order_type'] in ['hedge', 'hedge_tp'] or o['filled_amount'] > 0 or o['status'] == 'new':
        print(f"  [{ts(o['created_at'])}] type={o['order_type']} amt={o['amount']} fill={o['filled_amount']} status={o['status']} notes={str(o['notes'])[:40]}")

print("\nActive Orders in DB for BTC (10022):")
c.execute("SELECT order_type, amount, status FROM bot_orders WHERE bot_id=10022 AND status IN ('new','open','PARTIALLY_FILLED')")
for o in c.fetchall():
    print(f"  type={o['order_type']} amt={o['amount']} status={o['status']}")

conn.close()
