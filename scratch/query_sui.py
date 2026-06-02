import sqlite3

def run_sui_queries():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    
    # Query 1
    query1 = """
    SELECT b.id, b.name, b.direction, b.status, b.is_active,
           t.open_qty, t.total_invested, t.cycle_phase, t.cycle_id
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.pair LIKE '%SUI%' AND b.is_active = 1;
    """
    cur.execute(query1)
    rows1 = cur.fetchall()
    
    print("--- Bots and Trades SUI (is_active = 1) ---")
    cols1 = ['id', 'name', 'direction', 'status', 'is_active', 'open_qty', 'total_invested', 'cycle_phase', 'cycle_id']
    print(" | ".join(cols1))
    print("-" * 100)
    for r in rows1:
        print(" | ".join(str(val) for val in r))
        
    print("\n--- Active Positions SUI ---")
    query2 = """
    SELECT pair, side, size, entry_price 
    FROM active_positions WHERE pair LIKE '%SUI%';
    """
    cur.execute(query2)
    rows2 = cur.fetchall()
    cols2 = ['pair', 'side', 'size', 'entry_price']
    print(" | ".join(cols2))
    print("-" * 50)
    for r in rows2:
        print(" | ".join(str(val) for val in r))
        
    conn.close()

if __name__ == '__main__':
    run_sui_queries()
