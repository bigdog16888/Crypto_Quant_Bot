import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== RECONCILER-UNCREDITED SUMMARY ===')
for r in c.execute(
    "SELECT symbol, side, count(*), sum(qty), min(fill_ts), max(fill_ts) "
    "FROM exchange_fills WHERE source='reconciler-uncredited' GROUP BY symbol, side"
).fetchall():
    ts_min = __import__('datetime').datetime.fromtimestamp(r[4]) if r[4] else 'NULL'
    ts_max = __import__('datetime').datetime.fromtimestamp(r[5]) if r[5] else 'NULL'
    print(f'  symbol={r[0]:<16} side={r[1]:<5} rows={r[2]:>3} sum_qty={r[3]:>10.4f} '
          f'ts_range: {ts_min} -> {ts_max}')
print()

print('=== DUPLICATE EXCHANGE_ORDER_IDS IN reconciler-uncredited ===')
dups = c.execute(
    "SELECT exchange_order_id, symbol, side, count(*), sum(qty) "
    "FROM exchange_fills WHERE source='reconciler-uncredited' "
    "GROUP BY exchange_order_id HAVING count(*) > 1"
).fetchall()
if dups:
    for d in dups:
        print(f'  DUPLICATE: order_id={d[0]} symbol={d[1]} side={d[2]} count={d[3]} sum_qty={d[4]}')
else:
    print('  No duplicate exchange_order_ids in reconciler-uncredited.')
print()

print('=== ALL reconciler-uncredited ROWS ===')
for r in c.execute(
    "SELECT id, exchange_order_id, client_order_id, symbol, side, qty, price, fill_ts, bot_id, cycle_id "
    "FROM exchange_fills WHERE source='reconciler-uncredited' ORDER BY fill_ts"
).fetchall():
    ts = __import__('datetime').datetime.fromtimestamp(r[7]) if r[7] else 'NULL'
    print(f'  id={r[0]:>5} order_id={r[1]} cid={r[2]} symbol={r[3]:<16} side={r[4]:<5} '
          f'qty={r[5]:>8.4f} price={r[6]:>10.4f} ts={ts} bot_id={r[8]} cycle_id={r[9]}')

conn.close()
