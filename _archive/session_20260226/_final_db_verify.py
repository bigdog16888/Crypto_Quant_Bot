import sqlite3
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== FINAL DB CHECK ===')
c.execute('SELECT COUNT(*) FROM trades WHERE total_invested > 1.0')
active = c.fetchone()[0]
print(f'Active Trades (> $1): {active}')

c.execute('SELECT COUNT(*) FROM bot_orders WHERE status="open"')
open_orders = c.fetchone()[0]
print(f'Open Orders: {open_orders}')

c.execute('SELECT id, name, status FROM bots WHERE is_active=1')
print('\n=== BOT STATUSES ===')
for row in c.fetchall():
    print(row)

conn.close()
