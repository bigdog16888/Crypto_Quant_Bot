import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT cycle_id FROM bots WHERE id=10020")
current_cycle = c.fetchone()[0]

print(f"Current cycle: {current_cycle}")

c.execute("SELECT cycle_id, COUNT(*), MAX(step) FROM bot_orders WHERE bot_id=10020 GROUP BY cycle_id ORDER BY cycle_id DESC LIMIT 5")
print("Cycle ID | Order Count | Max Step")
for r in c.fetchall():
    print(r)
