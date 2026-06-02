import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("=== QUERY 1: ALL BOT ORDERS FOR 100318 ===")
    q1 = """
    SELECT order_type, status, filled_amount, amount, price, cycle_id, created_at, client_order_id
    FROM bot_orders
    WHERE bot_id = 100318
    ORDER BY created_at ASC;
    """
    cursor.execute(q1)
    rows1 = cursor.fetchall()
    cols1 = [d[0] for d in cursor.description]
    print(" | ".join(cols1))
    print("-" * 120)
    for r in rows1:
        # Convert timestamp to human readable local datetime if possible, or just raw timestamp
        print(" | ".join(str(val) for val in r))
        
    print("\n=== QUERY 2: FILLED BOT ORDERS FOR 100318 ===")
    q2 = """
    SELECT order_type, status, filled_amount, amount, price, cycle_id, created_at, client_order_id  
    FROM bot_orders
    WHERE bot_id = 100318
    AND filled_amount > 0
    ORDER BY created_at ASC;
    """
    cursor.execute(q2)
    rows2 = cursor.fetchall()
    cols2 = [d[0] for d in cursor.description]
    print(" | ".join(cols2))
    print("-" * 120)
    for r in rows2:
        print(" | ".join(str(val) for val in r))
        
    conn.close()

if __name__ == '__main__':
    run()
