import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import sqlite3

conn = sqlite3.connect('snapshot_manual_resolution_20260917_131511.db')
c = conn.cursor()

print('=== CANDIDATE SNAPSHOT ACTIVE_POSITIONS ===')
for r in c.execute('SELECT bot_id, pair, side, contracts FROM active_positions').fetchall():
    print(' ', r)

print('=== CANDIDATE SNAPSHOT NON-ZERO TRADES ===')
for r in c.execute('SELECT bot_id, open_qty, position_side FROM trades WHERE open_qty != 0').fetchall():
    print(' ', r)

conn.close()
