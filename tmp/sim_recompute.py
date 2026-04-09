import sqlite3

def run_recompute(bot_id):
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    cycle_id = 1
    cursor.execute("""
        SELECT
            COALESCE(SUM(
                CASE 
                    WHEN bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') THEN bo.filled_amount
                    WHEN bo.order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl') THEN -bo.filled_amount
                    ELSE 0.0
                END
            ), 0.0) AS total_qty
        FROM bot_orders bo
        WHERE bo.bot_id  = ?
          AND bo.cycle_id = ?
          AND bo.filled_amount > 0
          AND bo.price > 0
          AND bo.status NOT IN ('placing', 'failed', 'auto_closed', 'reset_cleared')
    """, (bot_id, cycle_id))
    
    qty = cursor.fetchone()[0]
    print(f"Phase 1 Qty for cycle {cycle_id}: {qty}")
    
    # Check carry
    carry_qty = cursor.execute("""
        SELECT COALESCE(SUM(filled_amount), 0.0)
        FROM bot_orders
        WHERE bot_id  = ?
          AND cycle_id = ?
          AND client_order_id LIKE 'CQB_%'
          AND client_order_id LIKE '%_CARRY_%'
          AND filled_amount > 0
          AND status NOT IN ('open', 'new', 'placing', 'failed')
    """, (bot_id, cycle_id)).fetchone()[0]
    
    print(f"Phase 2 Carry Qty for cycle {cycle_id}: {carry_qty}")
    conn.close()

run_recompute(10018)
