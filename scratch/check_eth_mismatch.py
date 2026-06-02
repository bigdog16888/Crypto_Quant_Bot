import sqlite3
import pandas as pd

def run_queries():
    conn = sqlite3.connect("crypto_bot.db")
    
    q1 = """
    SELECT b.name, b.direction, b.bot_type,
           t.open_qty, t.total_invested, t.cycle_id,
           t.cycle_phase, t.entry_confirmed, t.current_step,
           CASE WHEN b.direction='LONG' THEN t.open_qty ELSE -t.open_qty END as signed_qty
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE (b.pair LIKE '%ETH%USDC%' OR b.pair LIKE '%ETHUSDC%')
    AND b.is_active >= 0
    ORDER BY b.name;
    """
    
    q2 = """
    SELECT * FROM active_positions
    WHERE pair LIKE '%ETH%';
    """
    
    q3 = """
    SELECT b.name, bo.order_type, bo.filled_amount,
           bo.price, bo.status, bo.step, bo.cycle_id,
           datetime(bo.created_at, 'unixepoch') as created
    FROM bot_orders bo JOIN bots b ON bo.bot_id = b.id
    WHERE (b.pair LIKE '%ETH%USDC%')
    AND bo.filled_amount > 0
    AND bo.status NOT IN ('reset_cleared','rejected','failed','cancelled','auto_closed')
    AND bo.created_at > (strftime('%s','now') - 7200)
    ORDER BY bo.created_at DESC;
    """
    
    print("=== QUERY 1: Virtual net per bot on ETH pairs ===")
    df1 = pd.read_sql_query(q1, conn)
    print(df1.to_string(index=False))
    print("\n" + "="*50 + "\n")
    
    print("=== QUERY 2: Physical positions on ETH ===")
    df2 = pd.read_sql_query(q2, conn)
    print(df2.to_string(index=False))
    print("\n" + "="*50 + "\n")
    
    print("=== QUERY 3: Recent ETH bot_orders fills (last 2 hours) ===")
    df3 = pd.read_sql_query(q3, conn)
    print(df3.to_string(index=False))
    print("\n" + "="*50 + "\n")
    
    conn.close()

if __name__ == '__main__':
    run_queries()
