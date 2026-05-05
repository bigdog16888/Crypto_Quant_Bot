from engine.database import get_connection
import pandas as pd

conn = get_connection()
# Sum ALL fills for BTCUSDC in bot_orders, regardless of bot_id or cycle_id
query = """
    SELECT bot_id, 
           SUM(CASE 
               WHEN order_type IN ('entry', 'grid', 'adoption', 'adoption_add') THEN filled_amount 
               WHEN order_type IN ('tp', 'close', 'exit', 'adoption_reduce', 'dust_close', 'sl') THEN -filled_amount 
               WHEN order_type = 'hedge' THEN filled_amount
               WHEN order_type = 'hedge_tp' THEN -filled_amount
               ELSE 0 END) as net_qty
    FROM bot_orders 
    WHERE (position_side = 'LONG' OR position_side IS NULL OR position_side = 'BOTH' OR position_side = '')
      AND bot_id IN (SELECT id FROM bots WHERE pair LIKE '%BTC%')
    GROUP BY bot_id
"""
# Wait, for One-Way mode, we should just sum everything for the pair.
query = """
    SELECT bot_id, 
           SUM(CASE 
               WHEN side='buy' THEN filled_amount 
               WHEN side='sell' THEN -filled_amount 
               ELSE (CASE WHEN order_type IN ('entry', 'grid', 'adoption', 'adoption_add', 'hedge_tp') THEN filled_amount ELSE -filled_amount END)
               END) as net_qty
    FROM bot_orders 
    WHERE bot_id IN (SELECT id FROM bots WHERE pair LIKE '%BTC%')
    GROUP BY bot_id
"""
# I'll check the columns in bot_orders again. It has 'filled_amount'.
# It DOES NOT have 'side' in the schema I saw earlier.
# It has 'order_type' and 'position_side'.
# Let's check the schema.
