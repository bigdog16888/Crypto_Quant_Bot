import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute("UPDATE bots SET status='Scanning' WHERE status='REQUIRE_MANUAL_PROOF'")
print(f'Unlocked {cur.rowcount} bots.')
conn.commit()
conn.close()
