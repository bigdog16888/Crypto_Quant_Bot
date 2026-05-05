import sqlite3
import datetime

conn = sqlite3.connect('crypto_bot.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT order_type, amount, filled_amount, status, created_at, cycle_id, notes FROM bot_orders WHERE bot_id=10022 ORDER BY created_at DESC LIMIT 20")
print("RECENT BOT_ORDERS FOR BTC:")
for o in c.fetchall():
    dt = datetime.datetime.fromtimestamp(o['created_at']).strftime('%Y-%m-%d %H:%M:%S')
    print(f"  {dt} (ts: {o['created_at']}): type={o['order_type']} amt={o['amount']} fill={o['filled_amount']} status={o['status']} cycle={o['cycle_id']} notes={str(o['notes'])[:50]}")

c.execute("SELECT current_step, open_qty, cycle_id, cycle_phase, last_exit_time FROM trades WHERE bot_id=10022")
trades = dict(c.fetchone())
if trades['last_exit_time']:
    trades['last_exit_time_dt'] = datetime.datetime.fromtimestamp(trades['last_exit_time']).strftime('%Y-%m-%d %H:%M:%S')
print("\nTRADES:", trades)

c.execute("SELECT * FROM reconciler_logs WHERE bot_id=10022 ORDER BY timestamp DESC LIMIT 5")
print("\nRECONCILER LOGS:")
for row in c.fetchall():
    dt = datetime.datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
    print(f"  {dt}: {row['action']} - {row['details']}")

conn.close()
