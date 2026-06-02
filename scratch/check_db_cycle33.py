import sqlite3

DB_PATH = 'crypto_bot.db'

def run_query():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    query = """
    SELECT order_type, status, filled_amount, price, cycle_id, created_at, client_order_id
    FROM bot_orders
    WHERE bot_id = 100002 AND cycle_id = 33
    ORDER BY created_at ASC;
    """
    
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        print("Columns: order_type, status, filled_amount, price, cycle_id, created_at, client_order_id")
        for row in rows:
            print(row)
    except Exception as e:
        print(f"Error executing query: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    run_query()
