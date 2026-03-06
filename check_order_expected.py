import sqlite3, json
conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()
c.execute('SELECT b.id, b.name, t.current_step, b.config FROM bots b JOIN trades t ON b.id=t.bot_id WHERE t.total_invested > 0')
bots = c.fetchall()
total_expected = 0
for b in bots:
    cfg = json.loads(b[3])
    max_steps = int(cfg.get('max_steps', 99))
    at_max = b[2] >= max_steps
    expected = 1 if at_max else 2
    total_expected += expected
    c.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id=? AND status='open'", (b[0],))
    open_db = c.fetchone()[0]
    print(f"Bot {b[0]} ({b[1]}) | Step: {b[2]}/{max_steps} | AtMax={'YES' if at_max else 'no'} | ExpectedOrders={expected} | DB Open Orders={open_db}")

print(f"\nTotal Expected: {total_expected}")
conn.close()
