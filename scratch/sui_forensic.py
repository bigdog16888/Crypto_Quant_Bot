from engine.database import get_connection
import pandas as pd

def forensic_sui():
    conn = get_connection()
    # 1. Check Bots and Trades
    print("--- BOTS & TRADES (SUI) ---")
    df_bots = pd.read_sql("""
        SELECT b.id, b.name, b.direction, t.cycle_id, t.open_qty, t.hedge_qty, t.wipe_wall_ts 
        FROM bots b 
        JOIN trades t ON b.id = t.bot_id 
        WHERE b.pair LIKE 'SUI%' AND b.is_active = 1
    """, conn)
    print(df_bots)

    # 2. Check Orders that contribute to the current cycle (post Wipe Wall)
    print("\n--- ACTIVE ORDERS (SUI) ---")
    for _, bot in df_bots.iterrows():
        bot_id = bot['id']
        wall_ts = bot['wipe_wall_ts'] or 0
        cycle_id = bot['cycle_id']
        print(f"\nOrders for Bot {bot_id} (Cycle {cycle_id}, Wall {wall_ts}):")
        df_orders = pd.read_sql(f"""
            SELECT id, order_type, status, filled_amount, price, created_at, cycle_id
            FROM bot_orders 
            WHERE bot_id = {bot_id} 
              AND status IN ('filled', 'closed', 'auto_closed', 'hedge_exited')
              AND (created_at >= {wall_ts} OR {wall_ts} == 0)
              AND cycle_id = {cycle_id}
        """, conn)
        print(df_orders)

    # 3. Check for Orphans (NULL cycle_id)
    print("\n--- ORPHAN ORDERS (SUI) ---")
    df_orphans = pd.read_sql("""
        SELECT bo.id, bo.bot_id, bo.order_type, bo.status, bo.filled_amount, bo.created_at
        FROM bot_orders bo
        JOIN bots b ON bo.bot_id = b.id
        WHERE b.pair LIKE 'SUI%' 
          AND bo.cycle_id IS NULL
          AND bo.status IN ('filled', 'closed', 'auto_closed')
    """, conn)
    print(df_orphans)

if __name__ == "__main__":
    forensic_sui()
