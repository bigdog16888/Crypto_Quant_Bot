import sqlite3

def check_short_orders():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT id, order_id, order_type, price, amount, filled_amount, status, step, cycle_id FROM bot_orders WHERE bot_id = 100000 ORDER BY created_at DESC LIMIT 5")
    rows = cur.fetchall()
    print("Recent bot_orders for bot 100000:")
    for r in rows:
        print(r)
    conn.close()

if __name__ == '__main__':
    check_short_orders()
