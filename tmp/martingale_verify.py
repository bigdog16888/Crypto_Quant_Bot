"""
Verify invested amounts make mathematical sense given each bot's base_size and martingale_multiplier.
Formula: total_invested at step N = base * sum(mult^i for i in 0..N-1)
"""
import sys, json
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
bots = conn.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.config,
           t.current_step, t.total_invested, t.avg_entry_price
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE t.total_invested > 0 AND b.is_active=1
    ORDER BY t.total_invested DESC
""").fetchall()
conn.close()

print(f"{'Bot':>6} {'Name':15} {'Pair':14} {'Base':>7} {'Mult':>5} {'Step':>5} {'Expected$':>11} {'Actual$':>11} {'Match?'}")
print("-"*90)

for row in bots:
    bot_id, name, pair, direction, config_str, step, invested, avg_entry = row
    try:
        cfg = json.loads(config_str) if config_str else {}
    except:
        cfg = {}
    
    base_size = float(cfg.get('base_order_size', cfg.get('base_size', 0)))
    mult = float(cfg.get('martingale_multiplier', cfg.get('multiplier', 2.0)))
    step = int(step or 0)
    invested = float(invested or 0)
    
    # Expected total invested = base * (1 + mult + mult^2 + ... + mult^(step-1))
    if base_size > 0 and step > 0:
        expected = base_size * sum(mult**i for i in range(step))
    else:
        expected = 0.0
    
    ratio = invested / expected if expected > 0 else 0
    match = "✅" if 0.8 <= ratio <= 1.2 else f"⚠️ ratio={ratio:.2f}x"
    
    print(f"{bot_id:>6} {str(name):15} {str(pair):14} ${base_size:>6.1f} {mult:>5.2f} {step:>5} ${expected:>10.2f} ${invested:>10.2f} {match}")
    print(f"       Config keys: {list(cfg.keys())[:6]}")
    print()
