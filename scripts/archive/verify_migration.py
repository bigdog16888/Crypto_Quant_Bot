import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cursor = conn.cursor()
cursor.execute("""
SELECT wipe_proof_source, COUNT(*) AS row_count
FROM bot_orders
WHERE status = 'reset_cleared'
GROUP BY wipe_proof_source
ORDER BY row_count DESC;
""")
rows = cursor.fetchall()
print("wipe_proof_source | row_count")
print("------------------|----------")
for r in rows:
    print(f"{str(r[0]):18} | {r[1]}")
conn.close()
