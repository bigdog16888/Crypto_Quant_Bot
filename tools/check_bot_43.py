import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Check bot status
cur.execute('SELECT id, name, pair, is_active FROM bots WHERE id=43')
bot = cur.fetchone()
print(f"Bot #{bot[0]}: {bot[1]}")
print(f"Pair: {bot[2]}")
print(f"Active: {'YES' if bot[3] else 'NO'}")
print()

# Check trades
cur.execute('SELECT * FROM trades WHERE bot_id=43 ORDER BY timestamp DESC LIMIT 5')
trades = cur.fetchall()
print(f"Recent Trades: {len(trades)}")
for t in trades:
    print(f"  {t}")
print()

# Check martingale_state
cur.execute('SELECT * FROM martingale_state WHERE bot_id=43')
state = cur.fetchone()
if state:
    print("Martingale State:")
    print(f"  Step: {state[2]}")
    print(f"  Total Invested: ${state[3]}")
    print(f"  Avg Entry: ${state[4]}")
    print(f"  Target TP: ${state[5]}")
else:
    print("No martingale state found")

conn.close()
