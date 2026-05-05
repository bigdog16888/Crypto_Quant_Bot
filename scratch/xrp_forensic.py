from engine.database import get_connection
import pandas as pd

def forensic_xrp():
    conn = get_connection()
    print("--- ACTIVE ORDERS (XRP) ---")
    df_orders = pd.read_sql("""
        SELECT id, order_type, status, order_id, filled_amount, created_at, cycle_id
        FROM bot_orders 
        WHERE bot_id = 10017
          AND status IN ('filled', 'closed', 'auto_closed')
          AND filled_amount > 0
    """, conn)
    print(df_orders)

if __name__ == "__main__":
    forensic_xrp()
