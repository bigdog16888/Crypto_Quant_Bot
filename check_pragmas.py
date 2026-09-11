import sqlite3
conn = sqlite3.connect('crypto_bot.db')
print('journal_mode:', conn.execute('PRAGMA journal_mode').fetchone()[0])
print('busy_timeout:', conn.execute('PRAGMA busy_timeout').fetchone()[0])
print('synchronous:', conn.execute('PRAGMA synchronous').fetchone()[0])
print('wal_autocheckpoint:', conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0])
print('cache_size:', conn.execute('PRAGMA cache_size').fetchone()[0])
conn.close()