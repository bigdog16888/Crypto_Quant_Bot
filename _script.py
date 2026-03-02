import sqlite3

c = sqlite3.connect('crypto_bot.db')
c.row_factory = sqlite3.Row
rows = c.execute("SELECT b.id, b.name, b.pair, b.direction, t.total_invested, t.current_step, t.target_tp_price, t.avg_entry_price FROM bots b JOIN trades t ON b.id = t.bot_id WHERE b.name IN ('Recovered_Bot_10012', 'Recovered_Bot_10013')")
for r in rows:
    print(dict(r))
