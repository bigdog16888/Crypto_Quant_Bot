from engine.database import get_connection
import pandas as pd

def simulate_monitor_net():
    conn = get_connection()
    
    # Mirroring monitor.py Line 527
    query_v = """
        SELECT b.id, b.pair, b.direction, t.open_qty, t.total_invested, t.avg_entry_price
        FROM bots b
        JOIN trades t ON b.id = t.bot_id
        WHERE b.is_active = 1
    """
    df_v = pd.read_sql(query_v, conn)
    
    virtual_qty_by_pair = {}
    virtual_net_by_norm = {}
    
    for _, row in df_v.iterrows():
        open_qty_v = float(row['open_qty'] or 0)
        invested = float(row['total_invested'] or 0)
        avg_price = float(row['avg_entry_price'] or 0)
        pair_key = row['pair'].replace('/', '').replace(':', '').replace('USDCUSDC', 'USDC') # simplistic norm
        # In monitor.py: _norm_universal(row['pair'])
        # Let's assume it works.
        side_key = str(row['direction']).upper()
        
        if open_qty_v > 0:
            qty_abs = open_qty_v
        elif invested > 0 and avg_price > 0:
            qty_abs = invested / avg_price
        else:
            qty_abs = 0
            
        composite_key = (pair_key, side_key)
        virtual_qty_by_pair[composite_key] = virtual_qty_by_pair.get(composite_key, 0.0) + qty_abs
        
    for (pk, sk), q in virtual_qty_by_pair.items():
        if "SUI" in pk:
            print(f"DEBUG: {pk} {sk} = {q}")
        virtual_net_by_norm[pk] = virtual_net_by_norm.get(pk, 0.0) + (q if sk == 'LONG' else -q)
        
    for p, v in virtual_net_by_norm.items():
        if "SUI" in p:
            print(f"System Net {p}: {v}")

if __name__ == "__main__":
    simulate_monitor_net()
