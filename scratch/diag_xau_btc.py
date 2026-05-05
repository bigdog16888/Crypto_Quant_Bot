import sqlite3, datetime

conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

def ts(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime('%H:%M:%S') if epoch else 'N/A'

print("=== XAUUSDT (10019) CHECK ===")
c.execute("SELECT t.open_qty, t.total_invested, b.status FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.id=10019")
r = c.fetchone()
print(f"Bot 10019: open_qty={r['open_qty']} invested={r['total_invested']} status={r['status']}")

print("\nRecent bot_orders (last 10):")
c.execute("""
    SELECT id, order_type, order_id, amount, filled_amount, status, created_at, notes, cycle_id
    FROM bot_orders WHERE bot_id=10019 ORDER BY created_at DESC LIMIT 10
""")
for o in c.fetchall():
    print(f"  [{ts(o['created_at'])}] type={o['order_type']} amt={o['amount']} fill={o['filled_amount']} status={o['status']} notes={str(o['notes'])[:50]}")

print("\nRecent recon logs (XAU):")
c.execute("""
    SELECT timestamp, action, details FROM reconciliation_logs
    WHERE bot_id=10019 ORDER BY timestamp DESC LIMIT 5
""")
for l in c.fetchall():
    print(f"  [{ts(l['timestamp'])}] {l['action']}: {l['details']}")

print("\n=== BTC SHORT (10022) CHECK ===")
c.execute("SELECT t.open_qty, t.total_invested, b.status FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.id=10022")
r = c.fetchone()
print(f"Bot 10022: open_qty={r['open_qty']} invested={r['total_invested']} status={r['status']}")

print("\nRecent bot_orders for BTC (10022):")
c.execute("""
    SELECT id, order_type, order_id, amount, filled_amount, status, created_at, notes, cycle_id
    FROM bot_orders WHERE bot_id=10022 ORDER BY created_at DESC LIMIT 15
""")
for o in c.fetchall():
    print(f"  [{ts(o['created_at'])}] type={o['order_type']} amt={o['amount']} fill={o['filled_amount']} status={o['status']} notes={str(o['notes'])[:50]}")

print("\nRecent recon logs (BTC):")
c.execute("""
    SELECT timestamp, action, details FROM reconciliation_logs
    WHERE bot_id=10022 ORDER BY timestamp DESC LIMIT 5
""")
for l in c.fetchall():
    print(f"  [{ts(l['timestamp'])}] {l['action']}: {l['details']}")

conn.close()
