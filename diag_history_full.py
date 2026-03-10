import sqlite3
import datetime

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Get events before 14:00 today (14:00 PST = roughly timestamp of the logs)
# Actually let's just ORDER BY timestamp DESC, filter out WS_GRID_PARTIAL
c.execute("SELECT timestamp, action, pnl, notes FROM trade_history WHERE bot_id=10020 AND action != 'WS_GRID_PARTIAL' ORDER BY timestamp DESC LIMIT 20")
print("--- LAST 20 SIGNIFICANT TRADE EVENTS ---")
for r in c.fetchall():
    ts, action, pnl, notes = r
    dt = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{dt} | {action} | ${pnl} | {notes}")

c.execute("SELECT timestamp, action, details FROM reconciliation_logs WHERE bot_id=10020 ORDER BY timestamp DESC LIMIT 20")
print("--- LAST 20 RECON LOGS ---")
for r in c.fetchall():
    ts, action, details = r
    dt = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{dt} | {action} | {details}")
