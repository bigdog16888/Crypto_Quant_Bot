"""Check POSITION_IMPORT and AUTO_ADOPT for wrong direction"""
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()

# Get details of POSITION_IMPORT and AUTO_ADOPT
cur.execute("""
    SELECT id, action, symbol, price, amount, cost, notes, timestamp
    FROM trade_history 
    WHERE bot_id = 44 AND action IN ('POSITION_IMPORT', 'AUTO_ADOPT', 'ENTRY_RECOVERED')
    ORDER BY id
""")
imports = cur.fetchall()
print("BOT 44 - POSITION IMPORTS/ADOPTS:")
for i in imports:
    print(f"  {i[7]}: {i[1]} | {i[2]} | ${i[3]} x {i[4]} = ${i[5]} | {i[6]}")

# Now check import_position_from_exchange function usage
print("\n" + "=" * 80)
print("Checking database.py for import_position_from_exchange")
print("=" * 80)

conn.close()

# Read the function
with open('engine/database.py', 'r') as f:
    content = f.read()
    
# Find the function
import re
match = re.search(r'def import_position_from_exchange\([^)]+\):[^}]+?(?=\ndef |\Z)', content, re.DOTALL)
if match:
    print(match.group(0)[:1500])
