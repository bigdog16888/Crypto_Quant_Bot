import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("=== Bot 10021 in bots ===")
    cursor.execute("SELECT id, name, status, is_active FROM bots WHERE id = 10021")
    print(cursor.fetchone())
    
    print("\n=== Bot 10021 in trades ===")
    cursor.execute("SELECT * FROM trades WHERE bot_id = 10021")
    cols = [d[0] for d in cursor.description]
    row = cursor.fetchone()
    if row:
        for c, v in zip(cols, row):
            print(f"  {c}: {v}")
            
    print("\n=== Bot 10021 orders in current cycle ===")
    cursor.execute("SELECT order_id, client_order_id, order_type, price, amount, filled_amount, status, cycle_id, created_at, updated_at FROM bot_orders WHERE bot_id = 10021 ORDER BY created_at DESC LIMIT 10")
    for r in cursor.fetchall():
        print(r)
        
    conn.close()

if __name__ == '__main__':
    run()
