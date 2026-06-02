import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT order_type, status, filled_amount, amount, price, cycle_id, created_at, client_order_id, notes
        FROM bot_orders
        WHERE bot_id = 10018
        ORDER BY id DESC LIMIT 20
    """)
    print("=== RECENT ORDERS FOR BOT 10018 (sui long) ===")
    for r in cursor.fetchall():
        print(r)
    conn.close()

if __name__ == '__main__':
    run()
