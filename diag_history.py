import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print("--- TRADE HISTORY ---")
c.execute("SELECT timestamp, action, symbol, price, amount, pnl, notes FROM trade_history WHERE bot_id=10020 ORDER BY timestamp DESC LIMIT 5")
for r in c.fetchall(): print(r)

print("\n--- RECONCILIATION LOGS ---")
c.execute("SELECT timestamp, action, details FROM reconciliation_logs WHERE bot_id=10020 ORDER BY timestamp DESC LIMIT 5")
for r in c.fetchall(): print(r)
