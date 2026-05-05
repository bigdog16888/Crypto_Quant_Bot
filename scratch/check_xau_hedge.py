import sqlite3
import pandas as pd

def verify_hedge_accounting():
    conn = sqlite3.connect('crypto_bot.db')
    
    print("--- Hedge Accounting Verification ---")
    
    # 1. Check Bot 10019 (XAUUSDT) hedges
    bot_id = 10019
    query = """
        SELECT order_id, order_type, status, filled_amount, created_at 
        FROM bot_orders 
        WHERE bot_id = ? AND order_type IN ('hedge', 'hedge_tp')
        ORDER BY created_at DESC
    """
    df = pd.read_sql(query, conn, params=(bot_id,))
    
    if df.empty:
        print(f"No hedges found for Bot {bot_id}.")
    else:
        print(f"\nHedge History for Bot {bot_id}:")
        print(df.to_string(index=False))
        
        # Simulate the new reconciler query
        statuses = ('filled', 'closed', 'hedge_exited', 'reset_cleared', 'auto_closed')
        sum_query = f"""
            SELECT
                COALESCE(SUM(CASE WHEN order_type='hedge' THEN filled_amount ELSE 0 END), 0) as h_sum,
                COALESCE(SUM(CASE WHEN order_type='hedge_tp' THEN filled_amount ELSE 0 END), 0) as tp_sum
            FROM bot_orders
            WHERE bot_id={bot_id} AND status IN {statuses}
              AND order_type IN ('hedge','hedge_tp')
        """
        row = conn.execute(sum_query).fetchone()
        h_sum, tp_sum = row
        outstanding = h_sum - tp_sum
        print(f"\nCalculated Outstanding Hedge: {outstanding:.6f}")
        print(f"Logic used: status IN {statuses}")
        
        if abs(outstanding - 0.126) < 0.001:
            print("✅ SUCCESS: Found the historical hedge (0.126) even after reset_cleared!")
        else:
            print(f"ℹ️ Outstanding hedge is {outstanding:.6f}. (Expected 0.126 if testing persistence).")

    conn.close()

if __name__ == "__main__":
    verify_hedge_accounting()
