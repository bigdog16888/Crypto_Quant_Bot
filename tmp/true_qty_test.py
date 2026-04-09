import sqlite3

def run_test():
    conn = sqlite3.connect('crypto_bot.db')
    c = conn.cursor()
    c.execute("""
        SELECT order_type, filled_amount, created_at, status 
        FROM bot_orders 
        WHERE bot_id=10018 AND cycle_id=1 
        AND created_at <= 1774914031
    """)
    for r in c.fetchall():
        print(r)
    conn.close()

run_test()
