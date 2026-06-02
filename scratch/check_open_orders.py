import sqlite3

def check_open_orders():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("SELECT id, order_id, order_type, price, amount, status FROM bot_orders WHERE bot_id = 100313 AND status IN ('new', 'open', 'partially_filled')")
    rows = cur.fetchall()
    print("Open Orders for bot 100313:")
    if not rows:
        print("  NO ORDERS")
    else:
        for r in rows:
            print(f"  ID: {r[0]} | OID: {r[1]} | Type: {r[2]} | Price: {r[3]} | Amount: {r[4]} | Status: {r[5]}")
    conn.close()

if __name__ == '__main__':
    check_open_orders()
