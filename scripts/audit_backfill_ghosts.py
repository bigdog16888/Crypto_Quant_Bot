import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== BACKFILL ROWS BY SYMBOL (first 5 each) ===')
symbols = c.execute(
    "SELECT DISTINCT symbol FROM exchange_fills WHERE source='backfill'"
).fetchall()
for (sym,) in symbols:
    rows = c.execute(
        "SELECT id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at "
        "FROM exchange_fills WHERE symbol=? AND source='backfill' ORDER BY id ASC LIMIT 5",
        (sym,)
    ).fetchall()
    cnt = c.execute(
        "SELECT count(*), sum(case when side='BUY' then qty else -qty end) "
        "FROM exchange_fills WHERE symbol=? AND source='backfill'",
        (sym,)
    ).fetchone()
    print(f'\n  {sym}: backfill rows={cnt[0]}, backfill net={cnt[1]}')
    for r in rows:
        print('   ', r)

print()
print('=== ALL bot_ids IN backfill rows ===')
for row in c.execute(
    "SELECT DISTINCT bot_id FROM exchange_fills WHERE source='backfill' ORDER BY bot_id"
).fetchall():
    print('  bot_id:', row[0])

print()
print('=== bot 100000 IN bots table ===')
r = c.execute("SELECT id, pair, direction, is_active, status FROM bots WHERE id=100000").fetchone()
print('  ', r)

print()
print('=== SUSPICIOUS: fills where bot_id is NOT in bots table ===')
ghost_bot_ids = c.execute(
    "SELECT DISTINCT bot_id FROM exchange_fills "
    "WHERE bot_id NOT IN (SELECT id FROM bots) AND bot_id NOTNULL"
).fetchall()
print('  Ghost bot_ids in exchange_fills:', [r[0] for r in ghost_bot_ids])

conn.close()
