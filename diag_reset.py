import sqlite3
import datetime

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT timestamp, action, pnl, notes FROM trade_history WHERE bot_id=10020 AND action IN ('TP', 'EE', 'SL', 'MANUAL_CLOSE', 'GHOST_RESET', 'PHANTOM_RESET') ORDER BY timestamp DESC LIMIT 10")
for r in c.fetchall():
    ts, action, pnl, notes = r
    dt = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    print(f"{dt} | {action} | PnL: ${pnl} | {notes}")
