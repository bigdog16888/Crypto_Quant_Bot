import sqlite3, socket
print('=== PORT 19888 CHECK ===')
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
result = s.connect_ex(('127.0.0.1', 19888))
s.close()
print(f'Port 19888: {"IN USE" if result == 0 else "FREE"} (connect_ex={result})')
print()
print('=== CURRENT DB ROW COUNTS ===')
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
for tbl in ['exchange_fills', 'trades', 'bots', 'active_positions']:
    cnt = c.execute(f'SELECT count(*) FROM {tbl}').fetchone()[0]
    print(f'{tbl:<18}: {cnt}')
conn.close()
