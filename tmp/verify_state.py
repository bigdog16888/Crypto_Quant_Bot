import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== ADOPTION ORDER STATUS FOR ALL BOTS ===")
q.execute("""
    SELECT b.id, b.name, bo.order_type, bo.status, bo.filled_amount, bo.cycle_id
    FROM bot_orders bo
    JOIN bots b ON bo.bot_id = b.id
    WHERE bo.order_type IN ('adoption', 'adoption_add')
    ORDER BY b.id, bo.created_at DESC
""")
for row in q.fetchall():
    print(row)

print("\n=== TRADES STATE (entry_confirmed) ===")
q.execute("""
    SELECT b.id, b.name, t.total_invested, t.entry_confirmed, t.avg_entry_price, t.current_step
    FROM bots b
    JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY b.id
""")
for row in q.fetchall():
    status = "✅ IN TRADE" if (row[2] > 0 and row[3] == 1) else ("⚠️ invested but !confirmed" if (row[2] > 0 and row[3] == 0) else "🟡 Scanning")
    print(f"  {status}  bot={row[0]} ({row[1]}) invested=${row[2]:.2f} confirmed={row[3]} avg={row[4]:.2f} step={row[5]}")

c.close()
