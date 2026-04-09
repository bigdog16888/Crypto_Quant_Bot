"""Check recompute query math manually for SOL/Gold."""
import sys; sys.path.insert(0, '.')
from engine.database import get_connection

conn = get_connection()
bots = [10008, 10019]

for bot_id in bots:
    print(f"\n--- Bot {bot_id} ---")
    row = conn.execute("SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
    cycle_id = row[0]
    
    # Run the exact query from database.py
    res = conn.execute("""
        SELECT
            COALESCE(SUM(
                CASE 
                    WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN (filled_amount * price)
                    WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -(filled_amount * price)
                    ELSE 0.0
                END
            ), 0.0) AS total_cost,
            COALESCE(SUM(
                CASE 
                    WHEN order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN filled_amount
                    WHEN order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -filled_amount
                    ELSE 0.0
                END
            ), 0.0) AS total_qty,
            COALESCE(MAX(step), 0) AS max_step
        FROM bot_orders
        WHERE bot_id  = ?
          AND cycle_id = ?
          AND filled_amount > 0
          AND price > 0
          AND client_order_id LIKE 'CQB_%'
          AND client_order_id NOT LIKE '%_CARRY_%'
          AND status NOT IN ('placing', 'failed', 'auto_closed')
    """, (bot_id, cycle_id)).fetchone()
    
    print(f"Cycle {cycle_id} Query result: cost={res[0]:.4f}, qty={res[1]:.4f}, step={res[2]}")
    
    # Also let's break down by order type
    breakdown = conn.execute("""
        SELECT order_type, SUM(filled_amount * price), SUM(filled_amount), COUNT(*)
        FROM bot_orders
        WHERE bot_id  = ?
          AND cycle_id = ?
          AND filled_amount > 0
          AND price > 0
          AND client_order_id LIKE 'CQB_%'
          AND client_order_id NOT LIKE '%_CARRY_%'
          AND status NOT IN ('placing', 'failed', 'auto_closed')
        GROUP BY order_type
    """, (bot_id, cycle_id)).fetchall()
    print("Breakdown:")
    for b in breakdown:
        op = "+" if b[0] in ('entry', 'grid', 'adoption_add', 'adoption') else "-" 
        print(f"  {op} {b[0]}: cost={b[1]:.4f}, qty={b[2]:.4f}, count={b[3]}")

conn.close()
