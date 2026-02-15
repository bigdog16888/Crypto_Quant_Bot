import sqlite3
import time

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Check if bot 43 has entered
cur.execute('SELECT bot_id, current_step, total_invested, avg_entry_price FROM trades WHERE bot_id=43')
trade = cur.fetchone()

print(f"Bot #43 Trade Status (as of {time.strftime('%H:%M:%S')}):")
if trade:
    print(f"  Step: {trade[1]}")
    print(f"  Invested: ${trade[2]}")
    print(f"  Entry Price: ${trade[3]}")
    
    if trade[2] > 0:
        print("\n✅ BOT HAS ENTERED A TRADE!")
    else:
        print("\n❌ No position yet (waiting for entry)")
else:
    print("  No trade record")

# Check recent trade logs
cur.execute('''
    SELECT action, symbol, price, amount, notes, timestamp 
    FROM trade_log 
    WHERE bot_id=43 
    ORDER BY timestamp DESC 
    LIMIT 5
''')
logs = cur.fetchall()

if logs:
    print("\nRecent Trade Log Entries:")
    for log in logs:
        action, symbol, price, amt, notes, ts = log
        time_str = time.strftime('%H:%M:%S', time.localtime(ts))
        print(f"  [{time_str}] {action} {symbol} @ ${price} - {notes}")
else:
    print("\nNo trade log entries found")

conn.close()
