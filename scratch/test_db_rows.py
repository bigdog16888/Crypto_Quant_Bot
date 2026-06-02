import sqlite3

def run():
    conn = sqlite3.connect('crypto_bot.db')
    cur = conn.cursor()
    cur.execute(
        """
        SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0), b.is_active
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE b.pair LIKE '%SUI%'
        """
    )
    rows = cur.fetchall()
    print("SUI rows in DB:")
    for r in rows:
        print(r)
    conn.close()

if __name__ == '__main__':
    run()
