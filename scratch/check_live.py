import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    
    print("--- TRADES FOR ETH HEDGE BOTS ---")
    rows1 = cursor.execute("""
    SELECT b.id, b.name, t.open_qty, t.avg_entry_price, t.cycle_id, t.tp_order_id, b.status
    FROM trades t JOIN bots b ON b.id = t.bot_id
    WHERE b.name IN ('eth_hedge', 'long eth_hedge');
    """).fetchall()
    for r in rows1:
        print(r)
        
    print("\n--- RECENT BOT ORDERS ---")
    rows2 = cursor.execute("""
    SELECT bot_id, order_type, status, filled_amount, price, cycle_id, created_at, client_order_id, order_id
    FROM bot_orders
    WHERE bot_id IN (
        SELECT id FROM bots WHERE name IN ('eth_hedge', 'long eth_hedge')
    )
    ORDER BY created_at DESC LIMIT 10;
    """).fetchall()
    for r in rows2:
        print(r)

if __name__ == '__main__':
    run()
