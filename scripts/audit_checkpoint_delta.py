import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# active_positions: bot_id, pair, side, size, entry_price, last_checked(int), last_updated(text)
# side: 'LONG' or 'SHORT'  ->  sign: LONG=+1, SHORT=-1
# last_checked is the integer unix checkpoint timestamp.

pairs = [
    ('BNB/USDC:USDC',  'BNBUSDC'),
    ('BTC/USDC:USDC',  'BTCUSDC'),
    ('ETH/USDC:USDC',  'ETHUSDC'),
    ('LINK/USDC:USDC', 'LINKUSDC'),
    ('SOL/USDC:USDC',  'SOLUSDC'),
    ('SUI/USDC:USDC',  'SUIUSDC'),
    ('XAU/USDT:USDT',  'XAUUSDT'),
]

print('=== DYNAMIC CHECKPOINT + DELTA (signed base by side) ===')
hdr = f"{'norm':<10} | {'base':>9} | {'cp_ts':>10} | {'n_fill':>6} | {'delta':>9} | {'proj':>9}"
print(hdr)
print('-' * len(hdr))

for sym_full, sym_short in pairs:
    cp = c.execute(
        'SELECT side, size, last_checked FROM active_positions '
        'WHERE pair = ? OR pair = ?',
        (sym_full, sym_short)
    ).fetchone()

    if cp:
        side_str = cp[0]
        base_qty = float(cp[1])
        cp_ts = int(cp[2]) if cp[2] else 0
        sign = 1 if side_str.upper() == 'LONG' else -1
        base_signed = base_qty * sign
    else:
        base_signed = 0.0
        cp_ts = 0
        sign = 0  # unknown

    if cp_ts > 0:
        fills = c.execute(
            'SELECT side, qty, fill_ts FROM exchange_fills '
            'WHERE (symbol = ? OR symbol LIKE ?) AND fill_ts > ?',
            (sym_full, sym_short + '%', cp_ts)
        ).fetchall()
        delta_qty = sum(qty if s.upper() == 'BUY' else -qty for s, qty, ts in fills)
    else:
        fills = []
        delta_qty = 0.0  # no checkpoint -> no delta (backfill excluded by definition)

    projected = base_signed + delta_qty
    print(f'{sym_short:<10} | {base_signed:>+9.4f} | {cp_ts:>10} | {len(fills):>6} | {delta_qty:>+9.4f} | {projected:>+9.4f}')

print()
print('Base signed via active_positions.side (LONG=+, SHORT=-).')
print('No checkpoint (cp_ts=0) -> delta=0 (backfill rows excluded by construction).')
conn.close()
