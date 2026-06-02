import engine.database

conn = engine.database.get_connection()
cursor = conn.cursor()

target_cycle = 87
wall_ts = 0
bot_id = 100318
bot_side = 'SHORT'

cursor.execute(f"""
    SELECT 
        ROUND(COALESCE(SUM(
            CASE WHEN bo.cycle_id = ? AND bo.status NOT IN ('auto_closed', 'reset_cleared') AND (? = 0 OR bo.created_at >= ?)
                 AND bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption') 
            THEN (bo.filled_amount * bo.price) ELSE 0.0 END
        ), 0.0), 8) AS bought_cost,
        
        ROUND(COALESCE(SUM(
            CASE 
                WHEN bo.cycle_id = ? AND bo.status NOT IN ('auto_closed', 'reset_cleared') AND (? = 0 OR bo.created_at >= ?)
                     AND bo.order_type IN ('entry', 'grid', 'adoption_add', 'adoption', 'carry') 
                THEN bo.filled_amount ELSE 0.0 END
            ), 0.0), 8) AS bought_qty,
            
            COALESCE(SUM(
                CASE 
                    WHEN bo.cycle_id = ? AND bo.status NOT IN ('auto_closed', 'reset_cleared') AND (? = 0 OR bo.created_at >= ?)
                         AND bo.order_type IN ('adoption_reduce', 'tp', 'close', 'dust_close', 'sl', 'virtual_netting') 
                    THEN bo.filled_amount ELSE 0.0 END
            ), 0.0) AS sold_qty,
            
            COALESCE(MAX(CASE WHEN bo.cycle_id = ? AND (bo.created_at >= ? OR ? = 0) THEN bo.step ELSE 0 END), 0) AS max_step
            
        FROM bot_orders bo
        WHERE bo.bot_id = ?
          AND (
              bo.position_side = ? 
              OR bo.position_side IS NULL 
              OR bo.position_side = 'BOTH' 
              OR bo.position_side = ''
          )
          AND (
              bo.status IN ('filled', 'closed', 'auto_closed', 'hedge_exited', 'partially_filled')
              OR (bo.status IN ('canceled', 'cancelled') AND bo.filled_amount > 0)
          )
          AND bo.filled_amount > 0
    """, (
        target_cycle, wall_ts, wall_ts, # bought_cost
        target_cycle, wall_ts, wall_ts, # bought_qty
        target_cycle, wall_ts, wall_ts, # sold_qty
        target_cycle, wall_ts, wall_ts, # max_step
        bot_id, bot_side
    ))

res = cursor.fetchone()
print("bought_cost:", res[0])
print("bought_qty:", res[1])
print("sold_qty:", res[2])
print("max_step:", res[3])
