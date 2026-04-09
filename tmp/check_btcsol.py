import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== BTC/SOL BOT TRADES STATE ===")
q.execute("""
    SELECT b.id, b.name, b.direction, b.status, b.pair,
           t.total_invested, t.current_step, t.avg_entry_price, t.cycle_id, t.entry_confirmed
    FROM bots b
    LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.pair LIKE '%BTC%' OR b.pair LIKE '%SOL%'
    ORDER BY b.id
""")
for row in q.fetchall():
    print(row)

print("\n=== BTC/SOL RECENT bot_orders (last 10 each) ===")
for pair_like in ('%BTC%', '%SOL%'):
    q.execute("""
        SELECT b.name, bo.order_type, bo.status, bo.filled_amount, bo.cycle_id, bo.created_at
        FROM bot_orders bo
        JOIN bots b ON bo.bot_id = b.id
        WHERE b.pair LIKE ?
        ORDER BY bo.created_at DESC
        LIMIT 10
    """, (pair_like,))
    rows = q.fetchall()
    print(f"  Pair LIKE {pair_like}:")
    for row in rows:
        print(f"    {row}")

print("\n=== ACTIVE_POSITIONS for BTC/SOL ===")
q.execute("""
    SELECT * FROM active_positions WHERE pair LIKE '%BTC%' OR pair LIKE '%SOL%'
""")
for row in q.fetchall():
    print(row)

c.close()
