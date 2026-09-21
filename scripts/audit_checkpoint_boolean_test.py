import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print('=== BOOLEAN FILTER TEST: source != backfill vs fill_ts > cp_ts ===')
symbols = [
    ('SUI/USDC:USDC', 'SUIUSDC'),
    ('SOL/USDC:USDC', 'SOLUSDC'),
    ('BNB/USDC:USDC', 'BNBUSDC'),
]

for sym_full, sym_short in symbols:
    cp_row = c.execute(
        "SELECT side, size, last_checked FROM active_positions WHERE pair = ? OR pair = ?",
        (sym_full, sym_short)
    ).fetchone()
    cp_ts = int(cp_row[2]) if cp_row and cp_row[2] else 0
    base_size = float(cp_row[1]) if cp_row else 0.0
    base_side = str(cp_row[0]) if cp_row else 'UNKNOWN'
    sign = 1.0 if base_side.upper() == 'LONG' else -1.0

    all_fills = c.execute(
        "SELECT side, qty, source, fill_ts FROM exchange_fills WHERE symbol = ? ORDER BY fill_ts",
        (sym_full,)
    ).fetchall()

    delta_fill_ts = sum(q if str(s).upper()=='BUY' else -q for s,q,src,ts in all_fills if ts > cp_ts)
    delta_source = sum(q if str(s).upper()=='BUY' else -q for s,q,src,ts in all_fills if src != 'backfill')
    delta_both = sum(q if str(s).upper()=='BUY' else -q for s,q,src,ts in all_fills if ts > cp_ts and src != 'backfill')

    print(f'\n{sym_short} (base={base_size*sign:.4f}, cp_ts={cp_ts})')
    print(f'  ALL fills: {len(all_fills)} rows')
    print(f'  fill_ts > cp_ts: {sum(1 for *_,ts in all_fills if ts > cp_ts)} rows, delta={delta_fill_ts:.4f}')
    backfill_label = 'backfill'
    print(f'  source!=backfill: {sum(1 for *_,src,_ in all_fills if src != backfill_label)} rows, delta={delta_source:.4f}')
    print(f'  BOTH (ts>cp AND src!=backfill): delta={delta_both:.4f}')
    print(f'  Our impl (fill_ts>cp_ts) projected: {base_size*sign + delta_fill_ts:.4f}')

conn.close()
