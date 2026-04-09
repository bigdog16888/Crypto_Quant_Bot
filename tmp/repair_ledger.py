"""
Targeted ledger sync: aligns bot trades.total_invested + trades.avg_entry_price
to match the active_positions (exchange snapshot) for bots that have a real
physical position but stale/zeroed ledger data.

This is a one-time repair — NOT a wipe. It artificially adopts the exchange
position into the bot's ledger so the reconciler stops seeing a mismatch.

Run ONLY with the engine STOPPED or it will race.
"""
import sys
sys.path.insert(0, '.')
from engine.database import get_connection
import time

conn = get_connection()

print('=== PRE-FIX SNAPSHOT ===')

# Get all active_positions with their linked bots
ap_rows = conn.execute("""
    SELECT ap.pair, ap.side, ap.size, ap.entry_price, ap.bot_id,
           b.name, b.direction,
           COALESCE(t.total_invested, 0) as inv,
           COALESCE(t.current_step, 0) as step,
           COALESCE(t.avg_entry_price, 0) as avg_e,
           COALESCE(t.cycle_phase, 'ACTIVE') as phase
    FROM active_positions ap
    LEFT JOIN bots b ON ap.bot_id = b.id
    LEFT JOIN trades t ON ap.bot_id = t.bot_id
    ORDER BY ap.pair
""").fetchall()

fixes_applied = []

for row in ap_rows:
    pair, side, size, entry_price, bot_id, bname, bdir, inv, step, avg_e, phase = row
    size = float(size or 0)
    entry_price = float(entry_price or 0)
    inv = float(inv or 0)
    avg_e = float(avg_e or 0)
    
    exchange_notional = size * entry_price
    gap = abs(exchange_notional - inv)
    
    print(f"{pair} {side} | exchange={size:.4f} @ {entry_price:.6f} = ${exchange_notional:.2f} | bot {bot_id} ({bname}) inv=${inv:.2f} | GAP=${gap:.2f} | phase={phase}")
    
    # Only fix if gap > $5 AND bot has $0 invested (completely de-synced)
    # Do NOT fix dust gaps or direction mismatches manually
    if bot_id and bot_id != 0 and gap > 5.0 and inv < 1.0 and size > 0 and entry_price > 0:
        print(f"  --> APPLYING FIX: Setting bot {bot_id} ledger to match exchange ({size:.6f} @ {entry_price:.6f} = ${exchange_notional:.2f})")
        
        # Step is unknown — use 1 as minimum (bot was in trade)
        new_step = max(step, 1)
        
        conn.execute("""
            UPDATE trades
            SET total_invested = ?,
                avg_entry_price = ?,
                current_step = ?,
                entry_confirmed = 1,
                cycle_phase = 'ACTIVE',
                basket_start_time = COALESCE(NULLIF(basket_start_time, 0), ?)
            WHERE bot_id = ?
        """, (exchange_notional, entry_price, new_step, int(time.time()), bot_id))
        
        conn.execute("UPDATE bots SET status='IN TRADE' WHERE id=? AND status='Scanning'", (bot_id,))
        
        fixes_applied.append(f"Bot {bot_id} ({bname}) {pair}: ${inv:.2f} → ${exchange_notional:.2f}")

if fixes_applied:
    conn.commit()
    print()
    print(f'=== {len(fixes_applied)} FIXES APPLIED ===')
    for f in fixes_applied:
        print(f'  {f}')
else:
    print()
    print('=== NO FIXES NEEDED (all gaps < $5 or ledger already has invested amount) ===')

# Handle SOL bot_id=0 orphan — report it
sol_orphan = conn.execute("SELECT pair, side, size, entry_price FROM active_positions WHERE pair LIKE '%SOL%' AND bot_id=0").fetchone()
if sol_orphan:
    print()
    print(f'=== SOL ORPHAN (bot_id=0) ===')
    print(f'  {sol_orphan[0]} {sol_orphan[1]} size={sol_orphan[2]} @ {sol_orphan[3]}')
    print('  This position has no bot owner. If intentional, manually link it or close it from the UI.')

conn.close()
print()
print('DONE. Restart engine after reviewing output.')
