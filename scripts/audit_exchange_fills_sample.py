import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# 1. Discover actual schema for exchange_fills
print('=== exchange_fills SCHEMA ===')
for row in c.execute('PRAGMA table_info(exchange_fills)').fetchall():
    print(f'  col {row[0]}: {row[1]} ({row[2]})')
print()

# 2. Sample rows + net sum for each symbol
for sym in ['LINK/USDC:USDC', 'SUI/USDC:USDC', 'SOL/USDC:USDC']:
    print(f'=== exchange_fills FOR {sym} ===')
    # Find actual symbol column name
    col_info = c.execute("PRAGMA table_info(exchange_fills)").fetchall()
    sym_col = 'symbol'
    for ci in col_info:
        if ci[1] in ('symbol', 'pair', 'normal_pair', 'pair_normalized'):
            sym_col = ci[1]
            break

    rows = c.execute(
        f'SELECT id, {sym_col}, side, qty, price, source, bot_id, created_at '
        f'FROM exchange_fills WHERE {sym_col} = ? ORDER BY id ASC LIMIT 3',
        (sym,)
    ).fetchall()
    for r in rows:
        print('  ', r)

    # Net sum using side
    net = c.execute(
        f"SELECT count(*), sum(case when side='BUY' then qty else -qty end) "
        f"FROM exchange_fills WHERE {sym_col} = ?",
        (sym,)
    ).fetchone()
    print(f'   Total rows: {net[0]}, Net sum (BUY+, SELL-): {net[1]}')
    print()

conn.close()
