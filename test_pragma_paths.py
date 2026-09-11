import sqlite3

# Test 1: Current state of production DB
print("=== TEST 1: Production DB current state ===")
conn = sqlite3.connect('crypto_bot.db')
print('journal_mode:', conn.execute('PRAGMA journal_mode').fetchone())
print('synchronous:', conn.execute('PRAGMA synchronous').fetchone())
print('busy_timeout:', conn.execute('PRAGMA busy_timeout').fetchone())
conn.close()

# Test 2: Using engine.database.get_connection() 
print("\n=== TEST 2: engine.database.get_connection() path ===")
import sys
sys.path.insert(0, '.')
from engine.database import get_connection
conn = get_connection()
print('journal_mode:', conn.execute('PRAGMA journal_mode').fetchone())
print('synchronous:', conn.execute('PRAGMA synchronous').fetchone())
print('busy_timeout:', conn.execute('PRAGMA busy_timeout').fetchone())
conn.close()

# Test 3: Using init_db path
print("\n=== TEST 3: init_db() path ===")
from engine.database import init_db
init_db()
conn = sqlite3.connect('crypto_bot.db')
print('journal_mode:', conn.execute('PRAGMA journal_mode').fetchone())
print('synchronous:', conn.execute('PRAGMA synchronous').fetchone())
print('busy_timeout:', conn.execute('PRAGMA busy_timeout').fetchone())
conn.close()

print("\n=== All tests done ===")