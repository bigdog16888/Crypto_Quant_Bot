"""
audit_bot_state.py
-------------------
Read-only audit: compares what each bot CLAIMS to have invested (trades.total_invested)
vs what it ACTUALLY filled (sum of bot_orders filled records per current cycle).
Also fetches the real exchange position for comparison.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface, normalize_symbol

conn = get_connection()
cursor = conn.cursor()

# ── 1. Get all active bots with current trade state ──────────────────────────
cursor.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.is_active,
           COALESCE(t.total_invested, 0) AS claimed,
           COALESCE(t.avg_entry_price, 0) AS avg_entry,
           COALESCE(t.current_step, 0) AS step,
           COALESCE(t.entry_confirmed, 0) AS confirmed,
           COALESCE(t.basket_start_time, 0) AS basket_start,
           COALESCE(t.cycle_id, 1) AS cycle_id
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY b.pair, b.direction, b.id
""")
bots = cursor.fetchall()

print(f"\n{'='*100}")
print(f"{'BOT ID':<10} {'NAME':<30} {'PAIR':<12} {'DIR':<6} {'CLAIMED $':<12} {'FILL SUM $':<12} {'DIFF $':<10} {'STEP':<6} {'CONFIRMED':<10} {'STATUS'}")
print(f"{'='*100}")

by_pair = {}

for row in bots:
    bot_id, name, pair, direction, is_active, claimed, avg_entry, step, confirmed, basket_start, cycle_id = row

    # Sum fills from bot_orders for THIS bot's own orders (current cycle)
    cursor.execute("""
        SELECT COALESCE(SUM(price * amount), 0), COUNT(*)
        FROM bot_orders
        WHERE bot_id = ? AND status IN ('filled', 'closed', 'adoption')
        AND cycle_id = ?
    """, (bot_id, cycle_id))
    fill_row = cursor.fetchone()
    fill_sum = float(fill_row[0]) if fill_row else 0.0
    fill_count = int(fill_row[1]) if fill_row else 0

    diff = claimed - fill_sum

    # Flag status
    if claimed < 10.0:
        status = "SCANNING (idle)"
    elif abs(diff) < 20:
        status = "✅ CONSISTENT"
    elif fill_sum < 5 and claimed > 100:
        status = "🚨 PHANTOM (no fills but claims invested)"
    elif diff > 50:
        status = f"⚠️  INFLATED (claimed > fills by ${diff:.0f})"
    elif diff < -50:
        status = f"⬆️  UNDER-REPORTED (fills > claimed by ${abs(diff):.0f})"
    else:
        status = "~OK (small drift)"

    print(f"{bot_id:<10} {name[:29]:<30} {pair:<12} {direction:<6} {claimed:<12.2f} {fill_sum:<12.2f} {diff:<10.2f} {step:<6} {'YES' if confirmed else 'NO':<10} {status}")

    norm = normalize_symbol(pair)
    if norm not in by_pair:
        by_pair[norm] = {'LONG': 0.0, 'SHORT': 0.0}
    if direction.upper() == 'LONG':
        by_pair[norm]['LONG'] += claimed
    else:
        by_pair[norm]['SHORT'] += claimed

conn.close()

# ── 2. Compare pair-level virtual net vs physical exchange ───────────────────
print(f"\n{'='*100}")
print("PAIR-LEVEL VIRTUAL NET vs EXCHANGE")
print(f"{'='*100}")
print(f"{'PAIR':<12} {'VIRT LONG $':<14} {'VIRT SHORT $':<14} {'VIRT NET $':<14} {'EXCH NOTIONAL $':<18} {'DIFF $':<12} {'VERDICT'}")
print(f"{'-'*100}")

try:
    ex = ExchangeInterface(market_type='future')
    positions = ex.fetch_positions()
    exch_by_pair = {}
    for p in (positions or []):
        sym = normalize_symbol(p.get('symbol',''))
        size = float(p.get('contracts', 0) or 0)
        ep = float(p.get('entryPrice', 0) or 0)
        notional = abs(size) * ep
        side = 'LONG' if size > 0 else 'SHORT'
        if sym not in exch_by_pair:
            exch_by_pair[sym] = {'notional': 0.0, 'side': side}
        exch_by_pair[sym]['notional'] += notional

    all_pairs = set(list(by_pair.keys()) + list(exch_by_pair.keys()))
    for pair in sorted(all_pairs):
        virt_long = by_pair.get(pair, {}).get('LONG', 0.0)
        virt_short = by_pair.get(pair, {}).get('SHORT', 0.0)
        virt_net = virt_long - virt_short
        exch_notional = exch_by_pair.get(pair, {}).get('notional', 0.0)
        diff = abs(virt_net) - exch_notional
        if abs(diff) < 30:
            verdict = "✅ MATCHED"
        elif diff > 0:
            verdict = f"🚨 VIRTUAL INFLATED by ${diff:.2f}"
        else:
            verdict = f"⚠️  MISSING VIRTUAL (short ${abs(diff):.2f})"
        print(f"{pair:<12} {virt_long:<14.2f} {virt_short:<14.2f} {virt_net:<14.2f} {exch_notional:<18.2f} {diff:<12.2f} {verdict}")

except Exception as e:
    print(f"⚠️  Could not fetch exchange positions: {e}")
    print("   Bot-level virtual summary:")
    for pair, sides in sorted(by_pair.items()):
        virt_net = sides['LONG'] - sides['SHORT']
        print(f"   {pair}: LONG=${sides['LONG']:.2f}, SHORT=${sides['SHORT']:.2f}, NET=${virt_net:.2f}")

print(f"\n{'='*100}")
print("NOTE: This is READ-ONLY. Nothing was changed.")
print("Run 'python fix_bot_state.py' to apply corrections (stop bot first).")
print(f"{'='*100}\n")
