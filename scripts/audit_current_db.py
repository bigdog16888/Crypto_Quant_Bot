import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== CURRENT crypto_bot.db ACTIVE_POSITIONS ===')
for r in c.execute('SELECT bot_id, pair, side, contracts, entry_price FROM active_positions').fetchall():
    print(' ', r)

print('=== CURRENT crypto_bot.db NON-ZERO TRADES ===')
for r in c.execute('SELECT bot_id, open_qty, position_side, total_invested FROM trades WHERE open_qty != 0').fetchall():
    print(' ', r)

conn.close()
