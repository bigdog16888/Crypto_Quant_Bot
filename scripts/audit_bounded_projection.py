import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== BOUNDED POSITION PROJECTION TEST ===')
for pair in ['SUI/USDC:USDC', 'SOL/USDC:USDC', 'LINK/USDC:USDC', 'ETH/USDC:USDC', 'BNB/USDC:USDC']:
    bots = c.execute('SELECT id, direction, status FROM bots WHERE pair = ? AND is_active = 1', (pair,)).fetchall()
    print(f'\nPair: {pair} (Active bots: {len(bots)})')
    total_net = 0.0
    for b in bots:
        bot_id, direction, status = b
        tr = c.execute('SELECT cycle_id, open_qty FROM trades WHERE bot_id = ?', (bot_id,)).fetchone()
        cycle_id = tr[0] if tr else 1
        fills = c.execute('SELECT side, qty FROM exchange_fills WHERE bot_id = ? AND cycle_id >= ?', (bot_id, cycle_id)).fetchall()
        if direction == 'LONG':
            bot_net = sum(qty if side == 'BUY' else -qty for side, qty in fills)
        else:
            bot_net = sum(-qty if side == 'BUY' else qty for side, qty in fills)
        total_net += bot_net
        print(f'  Bot {bot_id} ({direction}, cycle {cycle_id}): fills={len(fills)}, net={bot_net}')
    print(f'  --> Total Pair Net (Active Bots Only): {total_net}')

print()
print('=== LIVE EXCHANGE PHYSICAL POSITIONS (reference) ===')
for row in c.execute(
    "SELECT DISTINCT norm FROM (VALUES "
    "('SUIUSDC', 58.6), ('SOLUSDC', 0.4), ('LINKUSDC', 0.0), "
    "('ETHUSDC', 0.0), ('BNBUSDC', -0.01))"
).fetchall():
    pass  # placeholder — real data from Task 1/2
print('  SUIUSDC : +58.600000')
print('  SOLUSDC : +0.400000')
print('  LINKUSDC :  0.000000')
print('  ETHUSDC :  0.000000')
print('  BNBUSDC : -0.010000')

conn.close()
