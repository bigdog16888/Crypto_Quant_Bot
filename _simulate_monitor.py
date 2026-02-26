import sqlite3
import json

conn = sqlite3.connect('crypto_bot.db')

def _norm(sym):
    s = str(sym).split(':')[0].strip()
    return s

# Simulate monitor.py _norm comparison
print('=== SIMULATING MONITOR MISMATCH LOGIC ===')
df_virt = conn.execute("""
    SELECT b.pair, t.total_invested, t.avg_entry_price, b.direction 
    FROM trades t
    JOIN bots b ON t.bot_id = b.id
    WHERE b.is_active = 1 AND t.total_invested > 0
""").fetchall()

virtual_by_pair = {}
virtual_gross = 0.0
for row in df_virt:
    amt_usd = row[1]
    virtual_gross += amt_usd
    pair_key = _norm(row[0])
    signed = amt_usd if row[3] == 'LONG' else -amt_usd
    virtual_by_pair[pair_key] = virtual_by_pair.get(pair_key, 0.0) + signed

df_phys = conn.execute("""
    SELECT pair, side, size, entry_price FROM active_positions
""").fetchall()

physical_by_pair = {}
for row in df_phys:
    val = row[2] * row[3]
    side = str(row[1]).upper().strip()
    pair_key = _norm(row[0])
    signed = abs(val) if side in ['BUY', 'LONG'] else -abs(val)
    physical_by_pair[pair_key] = physical_by_pair.get(pair_key, 0.0) + signed

print('Virtual by pair (after norm):')
for k, v in virtual_by_pair.items():
    print(f'  {k}: ${v:.2f}')

print('Physical by pair (after norm):')
for k, v in physical_by_pair.items():
    print(f'  {k}: ${v:.2f}')

all_pairs = set(list(virtual_by_pair.keys()) + list(physical_by_pair.keys()))
mismatched = []
for p in all_pairs:
    v = virtual_by_pair.get(p, 0.0)
    ph = physical_by_pair.get(p, 0.0)
    pair_diff = abs(v - ph)
    tolerance = max(5.0, 0.01 * max(abs(v), abs(ph)))
    status = 'MISMATCH ❌' if pair_diff > tolerance else 'OK ✅'
    print(f'  {p}: virtual={v:.2f} physical={ph:.2f} diff={pair_diff:.2f} tol={tolerance:.2f} → {status}')
    if pair_diff > tolerance:
        mismatched.append((p, v, ph, pair_diff))

print()
if mismatched:
    print(f'RESULT: 🚨 MISMATCH DETECTED ({len(mismatched)} pairs)')
else:
    print('RESULT: ✅ SYSTEM HEALTHY')

conn.close()
