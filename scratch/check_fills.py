import sqlite3
import datetime

def check_fills():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute("""
        SELECT id, order_id, order_type, price, amount, filled_amount, status, step, cycle_id, created_at, filled_at 
        FROM bot_orders 
        WHERE bot_id = 10018 AND cycle_id = 77 AND status = 'reset_cleared'
        ORDER BY created_at ASC
    """)
    rows = cur.fetchall()
    print("Cycle 77 fills for bot 10018:")
    for r in rows:
        created_dt = datetime.datetime.fromtimestamp(r[9]) if r[9] else 'None'
        filled_dt = datetime.datetime.fromtimestamp(r[10]) if r[10] else 'None'
        print(f"  ID: {r[0]} | OID: {r[1]} | Type: {r[2]} | Price: {r[3]} | Qty: {r[4]} | Status: {r[6]} | Step: {r[7]} | Created: {created_dt} | Filled: {filled_dt}")
    conn.close()

if __name__ == '__main__':
    check_fills()
