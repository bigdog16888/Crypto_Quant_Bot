import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("=== RECENT SUI BOT ORDERS ===")
    cursor.execute("""
        SELECT bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, notes
        FROM bot_orders
        WHERE bot_id IN (10018, 100000, 100318, 100323)
        ORDER BY id DESC LIMIT 15
    """)
    for r in cursor.fetchall():
        print(r)
        
    print("\n=== RECENT SUI TRADE HISTORY ===")
    cursor.execute("""
        SELECT *
        FROM trade_history
        WHERE bot_id IN (10018, 100000, 100318, 100323)
        ORDER BY rowid DESC LIMIT 15
    """)
    for r in cursor.fetchall():
        print(r)
        
    conn.close()

if __name__ == '__main__':
    run()
