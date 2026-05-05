import os
import sqlite3
import pandas as pd
from config.settings import config as global_config

def _local_norm(s):
    if not s: return ""
    return s.replace('/', '').replace('-', '').split(':')[0].upper()

def get_v_net_local(target_pair_norm, conn):
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT pair FROM bots")
        db_pairs = [r[0] for r in cursor.fetchall()]
        
        actual_db_pair = None
        for dbp in db_pairs:
            if _local_norm(dbp) == target_pair_norm:
                actual_db_pair = dbp
                break
        
        if not actual_db_pair: 
            print(f"DEBUG: Could not resolve {target_pair_norm} to any DB pair among {db_pairs}")
            return 0.0
        
        cursor.execute("""
            SELECT SUM(CASE WHEN b.direction = 'LONG' THEN t.open_qty ELSE -t.open_qty END)
            FROM bots b JOIN trades t ON b.id = t.bot_id
            WHERE b.pair = ? AND b.is_active = 1
        """, (actual_db_pair,))
        bot_net = cursor.fetchone()[0] or 0.0
        
        cursor.execute("""
            SELECT SUM(CASE 
                WHEN o.order_type = 'hedge' THEN (CASE WHEN b.direction = 'LONG' THEN -o.filled_amount ELSE o.filled_amount END)
                WHEN o.order_type = 'hedge_tp' THEN (CASE WHEN b.direction = 'LONG' THEN o.filled_amount ELSE -o.filled_amount END)
                ELSE 0 END)
            FROM bot_orders o JOIN bots b ON o.bot_id = b.id
            WHERE b.pair = ? AND o.order_type IN ('hedge', 'hedge_tp')
              AND o.status IN ('filled', 'closed', 'reset_cleared', 'auto_closed', 'hedge_exited')
        """, (actual_db_pair,))
        hedge_net = cursor.fetchone()[0] or 0.0
        
        return bot_net + hedge_net
    except Exception as e:
        print(f"Local Net Error for {target_pair_norm}: {e}")
        return 0.0

# Simulate monitor.py execution
db_path = global_config.PATHS['DB_FILE']
print(f"Connecting to: {db_path}")
conn = sqlite3.connect(db_path)

pairs = ['BTCUSDC', 'LINKUSDC', 'ETHUSDC', 'SOLUSDC']
for p in pairs:
    v_net = get_v_net_local(p, conn)
    print(f"{p}: System Net Qty = {v_net}")
