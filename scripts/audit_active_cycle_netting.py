import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== exchange_fills BY SOURCE ===')
for r in c.execute(
    'SELECT source, count(*), min(fill_ts), max(fill_ts) '
    'FROM exchange_fills GROUP BY source ORDER BY count(*) DESC'
).fetchall():
    ts_min = __import__('datetime').datetime.fromtimestamp(r[2]) if r[2] else 'NULL'
    ts_max = __import__('datetime').datetime.fromtimestamp(r[3]) if r[3] else 'NULL'
    print(f'  source={r[0]:<12} rows={r[1]:>5}  fill_ts range: {ts_min}  ->  {ts_max}')
print()

print('=== CURRENT ACTIVE CYCLE NETTING (algebraic: net = sum(BUY) - sum(SELL)) ===')
for pair in [
    'BNB/USDC:USDC', 'BTC/USDC:USDC', 'ETH/USDC:USDC',
    'LINK/USDC:USDC', 'SOL/USDC:USDC', 'SUI/USDC:USDC', 'XAU/USDT:USDT',
]:
    bots = c.execute(
        'SELECT id, direction, status FROM bots WHERE (pair = ? OR normalized_pair = ?) AND is_active = 1',
        (pair, pair)
    ).fetchall()
    pair_net = 0.0
    for b in bots:
        bot_id, direction, status = b
        tr = c.execute(
            'SELECT cycle_id, open_qty, position_side FROM trades WHERE bot_id = ?',
            (bot_id,)
        ).fetchone()
        if not tr or tr[1] == 0:
            continue
        cycle_id, open_qty, pos_side = tr
        fills = c.execute(
            'SELECT side, qty FROM exchange_fills WHERE bot_id = ? AND cycle_id = ?',
            (bot_id, cycle_id)
        ).fetchall()
        bot_net = sum(qty if side.upper() == 'BUY' else -qty for side, qty in fills)
        pair_net += bot_net
        print(
            f'  Bot {bot_id:>6} ({pos_side:<5}, cycle {cycle_id}): '
            f'open_qty={open_qty:>8.4f}  fills={len(fills):>3}  net={bot_net:>+10.4f}'
        )
    print(f'  --> Pair {pair:<18} Active Cycle Net: {pair_net:>+10.4f}')
print()

print('=== LIVE EXCHANGE PHYSICAL POSITIONS (from Task 1) ===')
print('  BNBUSDC : -0.010000')
print('  BTCUSDC : +0.002000')
print('  ETHUSDC :  0.000000')
print('  LINKUSDC :  0.000000')
print('  SOLUSDC : +0.400000')
print('  SUIUSDC : +58.600000')
print('  XAUUSDT : -0.068000')

conn.close()
