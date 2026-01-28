import sqlite3

conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Check bot
cur.execute('SELECT id, name, is_active FROM bots WHERE id=43')
row = cur.fetchone()
print(f'Bot #{row[0]}: {row[1]}')
print(f'Active: {"YES" if row[2] else "NO"}')

# Check trade
cur.execute('SELECT current_step, total_invested, avg_entry_price FROM trades WHERE bot_id=43')
trade = cur.fetchone()
if trade:
    print(f'\nTrade Status:')
    print(f'  Step: {trade[0]}')
    print(f'  Invested: ${trade[1]}')
    print(f'  Entry Price: ${trade[2]}')
    if trade[1] == 0:
        print('  Status: NO POSITION (waiting for entry)')
else:
    print('\nNo trade record found')

conn.close()
