"""Sync bots.status to 'IN TRADE' for all currently invested bots."""
import sqlite3

conn = sqlite3.connect('crypto_bot.db')
c = conn.cursor()

# Sync bots.status for all currently invested bots to 'IN TRADE'
c.execute("""
    UPDATE bots 
    SET status = 'IN TRADE'
    WHERE id IN (
        SELECT bot_id FROM trades 
        WHERE total_invested > 1.0
    )
    AND is_active = 1
""")
print("Updated", c.rowcount, "bots to IN TRADE status")

# Verify
c.execute("""
    SELECT b.id, b.name, b.status, COALESCE(t.total_invested,0) 
    FROM bots b LEFT JOIN trades t ON b.id=t.bot_id 
    WHERE b.is_active=1 ORDER BY b.id
""")
print()
print("=== BOT STATUS AFTER SYNC ===")
for r in c.fetchall():
    print("  Bot", r[0], "(" + r[1] + "):", "status=" + str(r[2]), " invested=$" + str(round(r[3],2)))

conn.commit()
conn.close()
print("Done.")
