import sqlite3, time

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

print("=== ACTIVE BOT STATES ===")
c.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.status, 
           t.total_invested, t.avg_entry_price, t.current_step, t.basket_start_time
    FROM bots b 
    LEFT JOIN trades t ON b.id = t.bot_id 
    WHERE b.is_active = 1
    ORDER BY t.total_invested DESC
""")
for r in c.fetchall():
    invested = r[5] or 0
    print(f"Bot {r[0]} ({r[1]}) | {r[3]} {r[2]} | status={r[4]} | invested=${invested:.2f} | step={r[7]} | BST={r[8]}")

print("\n=== OPEN ORDERS IN DB ===")
c.execute("""
    SELECT bo.bot_id, b.name, bo.order_type, bo.client_order_id, bo.price, bo.amount, bo.status 
    FROM bot_orders bo 
    JOIN bots b ON bo.bot_id = b.id 
    WHERE bo.status = 'open'
    ORDER BY bo.bot_id
""")
for r in c.fetchall():
    print(f"Bot {r[0]} ({r[1]}) | {r[2]} | {r[3]} | price={r[4]:.4f} | qty={r[5]:.4f}")

print("\n=== RECENT FILLS/EVENTS (last 20min) ===")
since = int(time.time()) - 1200
c.execute("""
    SELECT b.name, bo.order_type, bo.price, bo.amount, bo.filled_amount, bo.status, bo.created_at 
    FROM bot_orders bo 
    JOIN bots b ON bo.bot_id = b.id 
    WHERE bo.created_at > ? 
    ORDER BY bo.created_at DESC LIMIT 30
""", (since,))
for r in c.fetchall():
    print(f"{r[0]} | {r[1]} | price={r[2]:.4f} | qty={r[3]:.4f} | filled={r[4]:.4f} | status={r[5]} | at={r[6]}")

conn.close()
