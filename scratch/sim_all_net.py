from engine.database import get_connection
import pandas as pd

def simulate_all_net():
    conn = get_connection()
    query_v = """
        SELECT b.id, b.pair, b.direction, t.open_qty, t.total_invested, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
    """
    df_v = pd.read_sql(query_v, conn)
    
    virtual_net_by_norm = {}
    
    for _, row in df_v.iterrows():
        open_qty_v = float(row['open_qty'] or 0)
        pair_key = row['pair'].replace('/', '').replace(':', '').replace('USDCUSDC', 'USDC')
        side_key = str(row['direction']).upper()
        
        virtual_net_by_norm[pair_key] = virtual_net_by_norm.get(pair_key, 0.0) + (open_qty_v if side_key == 'LONG' else -open_qty_v)
        
    for p, v in virtual_net_by_norm.items():
        if abs(v) > 1e-8:
            print(f"System Net {p}: {v}")

if __name__ == "__main__":
    simulate_all_net()
