import sqlite3

def run_query():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    query = """
    SELECT b.pair, 
           SUM(CASE WHEN b.direction='LONG' THEN t.open_qty ELSE -t.open_qty END) as sys_net_qty
    FROM bots b JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active = 1 AND t.open_qty > 0
    GROUP BY b.pair
    ORDER BY b.pair;
    """
    cur.execute(query)
    rows = cur.fetchall()
    print("pair | sys_net_qty")
    print("-" * 30)
    for row in rows:
        print(f"{row[0]} | {row[1]}")
    conn.close()

if __name__ == '__main__':
    run_query()
