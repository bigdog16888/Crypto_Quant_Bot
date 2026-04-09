import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

c.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE bot_id = 0")
orphans = c.fetchall()

if not orphans:
    print("✅ No bot_id=0 orphans found in active_positions!")
else:
    print(f"⚠️ FOUND {len(orphans)} orphans:")
    for o in orphans:
        print(f"  Bot: {o[0]} | Pair: {o[1]} | Side: {o[2]} | Size: {o[3]}")

conn.close()
