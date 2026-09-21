import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.position_ledger import compute_bot_position, compute_pair_position
from engine.dataclasses import BotPosition
import sqlite3

conn = sqlite3.connect('crypto_bot.db')

print('=== DIRECT: compute_bot_position for SUI bot 10018 ===')
bp = compute_bot_position(10018, conn)
print(f'  bot_id={bp.bot_id} pair={bp.pair} direction={bp.direction}')
print(f'  net_qty={bp.net_qty}')
print(f'  entry_cost={bp.entry_cost}')
print(f'  avg_entry_price={bp.avg_entry_price}')
print(f'  realized_pnl={bp.realized_pnl}')
print(f'  fills_count={bp.fills_count}')
print()

print('=== DIRECT: compute_pair_position SUI/USDC:USDC ===')
pp = compute_pair_position('SUI/USDC:USDC', conn)
print(f'  pair={pp.pair} net_qty={pp.net_qty}')
print(f'  bots:')
for b in pp.bots:
    print(f'    bot {b.bot_id} ({b.direction}): net_qty={b.net_qty}, fills={b.fills_count}')
print()

# Isolate backfill contribution per SUI bot
print('=== SUI backfill vs all-fills per bot ===')
c = conn.cursor()
for bot_id in [10018, 100000]:
    all_rows = c.execute(
        "SELECT count(*), sum(case when side='BUY' then qty else -qty end) "
        "FROM exchange_fills WHERE bot_id=? AND symbol='SUI/USDC:USDC'",
        (bot_id,)
    ).fetchone()
    backfill_rows = c.execute(
        "SELECT count(*), sum(case when side='BUY' then qty else -qty end) "
        "FROM exchange_fills WHERE bot_id=? AND symbol='SUI/USDC:USDC' AND source='backfill'",
        (bot_id,)
    ).fetchone()
    real_rows = c.execute(
        "SELECT count(*), sum(case when side='BUY' then qty else -qty end) "
        "FROM exchange_fills WHERE bot_id=? AND symbol='SUI/USDC:USDC' AND source!='backfill'",
        (bot_id,)
    ).fetchone()
    print(f'  bot {bot_id}:')
    print(f'    ALL fills:     count={all_rows[0]}, net={all_rows[1]}')
    print(f'    backfill only: count={backfill_rows[0]}, net={backfill_rows[1]}')
    print(f'    real only:     count={real_rows[0]}, net={real_rows[1]}')
    print()

conn.close()
