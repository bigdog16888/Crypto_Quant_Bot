import sqlite3
conn = sqlite3.connect('crypto_bot.db', timeout=10)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""
    SELECT b.id, b.name, b.pair, b.direction, b.status as b_status,
           t.cycle_id, t.current_step, t.total_invested, t.open_qty, 
           t.avg_entry_price, t.entry_confirmed, t.wipe_wall_ts, t.basket_start_time
    FROM bots b LEFT JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active=1 ORDER BY b.pair, b.direction
""")
for r in cur.fetchall():
    r = dict(r)
    bid = r["id"]
    name = r["name"]
    pair = r["pair"]
    direction = r["direction"]
    status = r["b_status"]
    cycle = r["cycle_id"]
    step = r["current_step"]
    inv = r["total_invested"]
    qty = r["open_qty"]
    avg = r["avg_entry_price"]
    print(f"bot={bid:>6} {name:<22} {pair:<22} {direction:<6} status={status:<26} cycle={cycle} step={step} inv={inv} qty={qty} avg={avg}")
conn.close()
