import sqlite3
c = sqlite3.connect('crypto_bot.db')
q = c.cursor()
q.execute("SELECT b.id, b.name, b.status, t.total_invested, ap.bot_id, ap.size FROM bots b LEFT JOIN trades t ON b.id=t.bot_id LEFT JOIN active_positions ap ON b.id=ap.bot_id WHERE b.is_active=1")
print("ID | NAME | STATUS | INVESTED | AP_BOT_ID | AP_SIZE")
for r in q.fetchall():
    print(f"{r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]}")
c.close()
