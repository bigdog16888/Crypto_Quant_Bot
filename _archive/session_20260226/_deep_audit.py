import os
import sqlite3
import pandas as pd

def run_deep_audit():
    db_path = os.path.join(os.path.dirname(__file__), 'crypto_bot.db')
    conn = sqlite3.connect(db_path)
    
    # 1. Get virtual positions
    query_virtual = """
        SELECT b.id, b.name, b.pair, t.total_invested, t.avg_entry_price, t.current_step, b.direction 
        FROM trades t
        JOIN bots b ON t.bot_id = b.id
        WHERE b.is_active = 1 AND t.total_invested > 0
    """
    df_virt = pd.read_sql(query_virtual, conn)
    virtual_net = sum(row['total_invested'] if row['direction'] == 'LONG' else -row['total_invested'] for _, row in df_virt.iterrows())
    
    # 2. Get physical positions
    df_phys = pd.read_sql("SELECT pair, side, size, entry_price FROM active_positions", conn)
    physical_net = sum(abs(row['size'] * row['entry_price']) if row['side'].upper() in ['BUY', 'LONG'] else -abs(row['size'] * row['entry_price']) for _, row in df_phys.iterrows())
    
    print("="*60)
    print("VIRTUAL BOT POSITIONS (From Database)")
    print("="*60)
    print(df_virt.to_string())
    
    print("\n" + "="*60)
    print("PHYSICAL EXCHANGE POSITIONS (From Database Snapshot)")
    print("="*60)
    print(df_phys.to_string())
    
    print("\n" + "="*60)
    print("NET EXPOSURE CALCULATION")
    print("="*60)
    print(f"Virtual Net: ${virtual_net:,.2f}")
    print(f"Physical Net: ${physical_net:,.2f}")
    print(f"Gap: ${virtual_net - physical_net:,.2f} (Absolute Gap: ${abs(virtual_net - physical_net):,.2f})")
    
    # 3. Dump trade history for the active bots
    print("\n" + "="*60)
    print("RECENT DB TRADE HISTORY (Latest 15)")
    print("="*60)
    active_bot_ids = df_virt['id'].tolist()
    if active_bot_ids:
        placeholders = ','.join('?' for _ in active_bot_ids)
        history = pd.read_sql(f"""
            SELECT bot_id, action, round(price,2) as price, amount, round(pnl,2) as pnl, datetime(timestamp, 'unixepoch', 'localtime') as time 
            FROM trade_history 
            WHERE bot_id IN ({placeholders})
            ORDER BY timestamp DESC
            LIMIT 15
        """, conn, params=active_bot_ids)
        print(history.to_string())

if __name__ == "__main__":
    run_deep_audit()
