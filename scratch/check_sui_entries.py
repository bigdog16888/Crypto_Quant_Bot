import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT order_type, status, filled_amount, amount, price, cycle_id, created_at, client_order_id, notes
        FROM bot_orders
        WHERE bot_id = 10018 AND cycle_id = 87
        ORDER BY id ASC
    """)
    print("=== ALL ORDERS FOR BOT 10018 IN CYCLE 87 ===")
    for r in cursor.fetchall():
        print(r)
    conn.close()

if __name__ == '__main__':
    run()
