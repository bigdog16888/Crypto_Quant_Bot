from engine.database import get_connection
import pandas as pd

conn = get_connection()
query = "SELECT SUM(CASE WHEN side='BUY' THEN filled_amount ELSE -filled_amount END) as total_net FROM bot_orders WHERE bot_id = 10016 AND status IN ('filled', 'closed', 'reset_cleared');"
# Wait, I don't have side in bot_orders. I have order_type.
# Entry is BUY for LONG. TP is SELL for LONG.
query = """
    SELECT SUM(CASE 
        WHEN order_type='grid' THEN filled_amount 
        WHEN order_type='tp' THEN -filled_amount 
        ELSE 0 END) as total_net 
    FROM bot_orders 
    WHERE bot_id = 10016 AND status IN ('filled', 'closed', 'reset_cleared', 'auto_closed');
"""
print(pd.read_sql(query, conn))
