import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_connection, audit_pair_ledger_vs_exchange
import sqlite3

ex = ExchangeInterface(market_type='future')
conn = get_connection()

pairs = ['BNB/USDC:USDC', 'BTC/USDC:USDC', 'ETH/USDC:USDC', 'LINK/USDC:USDC', 'SOL/USDC:USDC', 'SUI/USDC:USDC', 'XAU/USDT:USDT']

# Live positions from exchange
print('=== LIVE EXCHANGE POSITIONS ===')
live = {}
for p in ex.fetch_positions():
    amt = float(p.get('contracts', 0) or p.get('size', 0) or 0)
    if abs(amt) > 1e-12:
        norm = normalize_symbol(p.get('symbol', '')).upper()
        live[norm] = amt
        print(f'  {norm:<12} contracts={amt:<10.4f} side={p.get("side")}')

# Mismatch audit
print()
print('=== STARTUP BARRIER AUDIT (CHECKPOINT + DELTA) ===')
mismatches = audit_pair_ledger_vs_exchange(ex)
mismatch_pairs = {m[0] for m in mismatches}

print(f'{"pair":<16} | {"status":<10} | {"virt":<10} | {"phys":<10} | {"delta":<10}')
print('-' * 70)
for pair in pairs:
    norm = normalize_symbol(pair).upper()
    phys = live.get(norm)
    bot_count = conn.execute(
        "SELECT count(*) FROM bots WHERE is_active=1 AND (pair=? OR normalized_pair=?)",
        (pair, pair)
    ).fetchone()[0]
    if pair in mismatch_pairs:
        m = [x for x in mismatches if x[0] == pair][0]
        status = 'MISMATCH'
        virt = m[1]
        delta = m[3]
    elif bot_count == 0:
        status = 'NO_BOTS'
        virt = 'N/A'
        delta = 'N/A'
        phys = phys if phys is not None else 'N/A'
    else:
        status = 'OK'
        virt = phys
        delta = 0.0
    print(f'{pair:<16} | {status:<10} | {str(virt):<10} | {str(phys):<10} | {str(delta):<10}')

conn.close()
print()
print('Mismatches:', len(mismatches))
for m in mismatches:
    print('  ', m[0], 'virt=', m[1], 'phys=', m[2], 'delta=', m[3])
