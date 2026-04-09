"""
Show the actual per-step fills for each active bot to understand
if the invested totals make sense given the order sizes placed.
"""
import sys, json
sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
bots = conn.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.config,
           t.current_step, t.total_invested, t.avg_entry_price, t.cycle_id
    FROM bots b JOIN trades t ON b.id=t.bot_id
    WHERE t.total_invested > 0 AND b.is_active=1
    ORDER BY t.total_invested DESC
""").fetchall()
conn.close()

for row in bots:
    bot_id, name, pair, direction, config_str, step, invested, avg_entry, cycle_id = row
    try:
        cfg = json.loads(config_str)
    except:
        cfg = {}
    
    base_grid = cfg.get('base_grid', 'N/A')
    grid_mult = cfg.get('GridMultiplier', 'N/A')
    
    conn = get_connection()
    orders = conn.execute("""
        SELECT order_type, step, filled_amount, price, filled_amount*price AS cost
        FROM bot_orders
        WHERE bot_id=? AND cycle_id=?
          AND order_type IN ('entry','grid','adoption_add','adoption','adoption_reduce')
          AND filled_amount > 0 AND price > 0
          AND status NOT IN ('open','new','placing','failed','auto_closed','reset_cleared')
        ORDER BY step ASC, created_at ASC
    """, (bot_id, cycle_id)).fetchall()
    conn.close()
    
    print(f"\n{'='*65}")
    print(f"Bot: {name} ({bot_id}) | {pair} {direction} | DB: step={step} ${float(invested):.2f} @ ${float(avg_entry):.4f}")
    print(f"Config: base_grid={base_grid}, GridMultiplier={grid_mult}")
    print(f"{'Type':15} {'Step':>5} {'Qty':>10} {'Price':>10} {'Cost':>10}")
    print(f"-"*55)
    
    running_total = 0.0
    for otype, fstep, famt, fprice, cost in orders:
        sign = "+" if otype != 'adoption_reduce' else "-"
        cost_signed = cost if otype != 'adoption_reduce' else -cost
        running_total += cost_signed
        print(f"{otype:15} {str(fstep or '?'):>5} {sign}{famt:>9.4f} ${fprice:>9.4f} ${cost_signed:>+9.2f}  (running: ${running_total:.2f})")
    
    diff = float(invested) - running_total
    print(f"\n  Orders SUM: ${running_total:.2f} | DB stored: ${float(invested):.2f} | Diff: ${diff:+.2f}")
    match = "✅ CLEAN" if abs(diff) < 1.0 else f"⚠️ GHOST = ${diff:+.2f}"
    print(f"  Status: {match}")
