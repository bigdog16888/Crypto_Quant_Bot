import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("--- Distinct cycles for bot 10016 in crypto_bot.db ---")
    rows = cursor.execute("SELECT DISTINCT cycle_id FROM bot_orders WHERE bot_id = 10016").fetchall()
    print(rows)

    print("\n--- All bot_orders for bot 10016 in crypto_bot.db ---")
    rows = cursor.execute("SELECT cycle_id, order_type, status, amount, price, client_order_id, created_at FROM bot_orders WHERE bot_id = 10016 ORDER BY created_at ASC").fetchall()
    for r in rows:
        print(r)

if __name__ == '__main__':
    run()
