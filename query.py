import sys
sys.path.append('c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot')
from dotenv import load_dotenv
load_dotenv()

from engine.database import get_connection, get_pair_virtual_net
from engine.exchange_interface import ExchangeInterface

conn = get_connection()
ex = ExchangeInterface(market_type='future')

print("=" * 70)
print("EXCHANGE PHYSICAL POSITIONS")
print("=" * 70)
positions = ex.fetch_positions()
exchange_map = {}
for p in positions:
    sym = p['symbol']
    net = p['net_qty']
    side = p['side']
    entry = p.get('entryPrice', 0)
    pnl = p.get('unrealizedPnl', 0)
    print(f"  {sym:25s}  net={net:+.4f}  side={side:5s}  entry={entry:.4f}  uPnL=${pnl:.2f}")
    # normalize key
    norm = sym.split(':')[0].replace('/', '').upper()
    exchange_map[norm] = net

print()
print("=" * 70)
print("SYSTEM VIRTUAL LEDGER (all active bots)")
print("=" * 70)
bots = conn.execute('''
    SELECT b.id, b.name, b.pair, b.direction,
           t.total_invested, t.open_qty, t.cycle_phase, t.current_step, t.cycle_id
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE b.is_active=1
    ORDER BY t.total_invested DESC
''').fetchall()

for b in bots:
    bid, name, pair, direction, invested, open_qty, phase, step, cycle = b
    status = "IN TRADE" if invested > 0.01 else "SCANNING"
    print(f"  [{bid:6d}] {name:20s} {pair:25s} {direction:5s} "
          f"${invested:8.2f}  qty={open_qty:.4f}  {phase:20s}  step={step}  cycle={cycle}  [{status}]")

print()
print("=" * 70)
print("PAIR-LEVEL RECONCILIATION: System vs Exchange")
print("=" * 70)

# Get all unique pairs from both sources
all_pairs = set()
for b in bots:
    norm = b[2].split(':')[0].replace('/', '').upper()
    all_pairs.add(norm)
for k in exchange_map:
    all_pairs.add(k)

print(f"  {'PAIR':15s}  {'SYS NET':>10s}  {'EX NET':>10s}  {'DIFF':>10s}  STATUS")
print(f"  {'-'*15}  {'-'*10}  {'-'*10}  {'-'*10}  ------")
all_ok = True
for pair_norm in sorted(all_pairs):
    # Use canonical pair form for get_pair_virtual_net
    # Find the canonical pair from bots table
    canonical = None
    for b in bots:
        if b[2].split(':')[0].replace('/', '').upper() == pair_norm:
            canonical = b[2]
            break
    if canonical is None:
        canonical = pair_norm  # fallback

    sys_net = get_pair_virtual_net(canonical) if canonical else 0.0
    ex_net = exchange_map.get(pair_norm, 0.0)
    diff = abs(sys_net - ex_net)
    ok = diff < 0.01
    if not ok:
        all_ok = False
    flag = "✅" if ok else "⚠️  MISMATCH"
    print(f"  {pair_norm:15s}  {sys_net:+10.4f}  {ex_net:+10.4f}  {diff:10.4f}  {flag}")

print()
if all_ok:
    print("✅ ALL PAIRS MATCH — System is tracking exchange correctly")
else:
    print("⚠️  MISMATCHES DETECTED — see above")
