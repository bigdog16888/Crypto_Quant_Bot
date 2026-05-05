import sqlite3

def dump_bot_orders(bot_id):
    conn = sqlite3.connect('crypto_bot.db')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM bot_orders WHERE bot_id=? AND cycle_id=11 ORDER BY id ASC", (bot_id,))
    rows = cur.fetchall()
    
    print(f"--- BOT ORDERS FOR BOT {bot_id} CYCLE 11 ---")
    for r in rows:
        print(f"ID={r['id']} step={r['step']} type={r['order_type']} amt={r['amount']} fill={r['filled_amount']} status={r['status']} notes={r['notes']}")

    conn.close()

dump_bot_orders(10022)
