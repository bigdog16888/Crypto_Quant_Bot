"""Check trade logs for recovery actions"""
import sqlite3
conn = sqlite3.connect('crypto_bot.db')
cur = conn.cursor()
cur.execute("""
    SELECT bot_id, action, notes, created_at 
    FROM trade_log 
    WHERE action LIKE '%RECOVERY%' OR action LIKE '%ADOPT%' OR notes LIKE '%recover%'
    ORDER BY created_at DESC LIMIT 20
""")
rows = cur.fetchall()
print("RECOVERY/ADOPT ACTIONS:")
for r in rows:
    print(f"  {r[3]}: Bot {r[0]} - {r[1]} - {r[2]}")
print(f"Total: {len(rows)}")
