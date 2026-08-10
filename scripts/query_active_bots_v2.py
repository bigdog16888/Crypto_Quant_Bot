import sys
import os

# Use the system Python 3.10 directly
sys.path.insert(0, r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot')

from engine import database

database.DB_PATH = r'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\crypto_bot.db'
database.close_connection()
conn = database.get_connection()

rows = conn.execute('''
    SELECT b.id, b.name, b.pair, b.direction, b.status
    FROM bots b
    WHERE b.is_active = 1
    ORDER BY b.id
''').fetchall()

print('=== ACTIVE BOTS ===')
active_bots = []
for r in rows:
    print(f'  Bot {r[0]}: {r[1]} | {r[2]} | {r[3]} | status={r[4]}')
    active_bots.append(r)

database.close_connection()
print('=== DB QUERY OK ===')