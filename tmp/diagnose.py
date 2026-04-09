"""
Precise mismatch root cause: shows exactly how the UI calculates
system_qty vs exchange_qty for each pair
"""
import sys
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()

for pair_filter in ['ETH', 'SUI', 'SOL', 'BNB']:
    print(f'\n=== {pair_filter} ===')
    
    # System side: what trades table says for all active bots on this pair
    sys_rows = conn.execute("""
        SELECT b.id, b.name, b.direction, b.status,
               COALESCE(t.total_invested, 0) as inv,
               COALESCE(t.avg_entry_price, 0) as avg_e,
               COALESCE(t.current_step, 0) as step,
               COALESCE(t.cycle_phase, 'ACTIVE') as phase
        FROM bots b
        LEFT JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active=1 AND b.pair LIKE ?
        ORDER BY b.direction
    """, (f'%{pair_filter}%',)).fetchall()
    
    long_total = 0.0
    short_total = 0.0
    for bid, name, dirn, status, inv, avg_e, step, phase in sys_rows:
        inv = float(inv or 0)
        avg_e = float(avg_e or 0)
        qty = inv / avg_e if avg_e > 0 else 0
        signed_qty = qty if dirn == 'LONG' else -qty
        if dirn == 'LONG': long_total += qty
        else: short_total += qty
        print(f"  SYSTEM Bot {bid} {name} | {dirn} | inv=${inv:.2f} | avg={avg_e:.4f} | qty={qty:.4f} | signed={signed_qty:.4f} | step={step} | phase={phase} | {status}")
    
    net_qty = long_total - short_total
    print(f"  SYSTEM NET QTY: +{long_total:.4f} LONG - {short_total:.4f} SHORT = {net_qty:+.4f}")
    
    # Exchange side: what active_positions table says
    exc_rows = conn.execute("""
        SELECT pair, side, size, entry_price, bot_id
        FROM active_positions
        WHERE pair LIKE ?
    """, (f'%{pair_filter}%',)).fetchall()
    
    exc_long = 0.0
    exc_short = 0.0
    for pair, side, size, price, bot_id in exc_rows:
        size = float(size or 0)
        price = float(price or 0)
        sgn = size if side in ('LONG', 'BUY') else -size
        if side in ('LONG', 'BUY'): exc_long += size
        else: exc_short += size
        print(f"  EXCHANGE {pair} {side} | size={size:.4f} @ {price:.4f} | bot_id={bot_id}")
    
    exc_net = exc_long - exc_short
    print(f"  EXCHANGE NET QTY: +{exc_long:.4f} LONG - {exc_short:.4f} SHORT = {exc_net:+.4f}")
    print(f"  DIFF: {net_qty - exc_net:+.4f}")

conn.close()
