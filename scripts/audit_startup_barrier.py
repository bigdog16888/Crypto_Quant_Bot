import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv()

from engine.exchange_interface import ExchangeInterface, normalize_symbol
from engine.database import get_connection, audit_pair_ledger_vs_exchange
from engine.startup_repair_verification import _mismatch_explainable_by_cid

ex = ExchangeInterface(market_type='future')
conn = get_connection()

# 1. Snapshot EVERY nonzero exchange position so we see foreign/stray symbols too
print('=== LIVE EXCHANGE POSITIONS (all nonzero) ===')
phys_by_norm = {}
for pos in ex.fetch_positions() or []:
    amt = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
    if abs(amt) < 1e-12:
        continue
    norm = normalize_symbol(pos.get('symbol', '')).upper()
    phys_by_norm[norm] = phys_by_norm.get(norm, 0.0) + amt

for norm, qty in sorted(phys_by_norm.items()):
    print(f'  {norm:<16} phys={qty:+.6f}')

print()

# 2. Run the pair audit (iterates active bot pairs from DB)
print('=== STARTUP BARRIER AUDIT — active bot pairs ===')
mismatches = audit_pair_ledger_vs_exchange(ex)

header = f"{'pair':<16} | {'virt':>10} | {'phys':>10} | {'delta':>10} | CID_explainable"
print(header)
print('-' * len(header))

if not mismatches:
    print('  (no mismatches returned — all active bot pairs in parity)')
else:
    for pair, virt, phys, delta in mismatches:
        norm = normalize_symbol(pair).upper()
        cid_ok = _mismatch_explainable_by_cid(pair, virt, phys, ex)
        print(f'{pair:<16} | {virt:>10.6f} | {phys:>10.6f} | {delta:>+10.6f} | {cid_ok}')

print()

# 3. Cross-reference: which exchange norms have NO active bots?
print('=== EXCHANGE NORMS WITH NO ACTIVE BOT (foreign/stray) ===')
cursor = conn.cursor()
db_norms = {r[0] for r in cursor.execute(
    "SELECT DISTINCT normalized_pair FROM bots WHERE is_active=1"
).fetchall()}
for norm, qty in sorted(phys_by_norm.items()):
    if norm not in db_norms:
        print(f'  {norm:<16} phys={qty:+.6f}  (no active bot)')

conn.close()
