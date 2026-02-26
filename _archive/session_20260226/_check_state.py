import sqlite3, json

conn = sqlite3.connect('crypto_bot.db')

print("=== ACTIVE BOTS WITH POSITIONS ===")
rows = conn.execute("""
    SELECT b.id, b.name, b.direction, b.status, 
           COALESCE(t.total_invested, 0) as invested,
           COALESCE(t.current_step, 0) as step,
           COALESCE(t.avg_entry_price, 0) as entry
    FROM bots b LEFT JOIN trades t ON b.id = t.bot_id
    WHERE b.is_active = 1
    ORDER BY invested DESC
""").fetchall()

total_invested = 0
for r in rows:
    bot_id, name, direction, status, invested, step, entry = r
    total_invested += invested
    if invested > 0 or status not in ('Scanning', 'Idle'):
        print(f"  {name}: {direction} ${invested:.2f} step={step} avg={entry:.2f} status={status}")

print(f"\nTotal virtual invested: ${total_invested:.2f}")

print("\n=== OPEN ORDERS IN DB ===")
open_orders = conn.execute("""
    SELECT b.name, bo.order_type, bo.price, bo.amount, bo.step, bo.status
    FROM bot_orders bo JOIN bots b ON bo.bot_id = b.id
    WHERE bo.status = 'open'
    ORDER BY b.name, bo.step
""").fetchall()
print(f"  {len(open_orders)} open orders tracked")
for r in open_orders[:20]:
    print(f"  {r[0]}: {r[1]} step={r[4]} price={r[2]:.2f} qty={r[3]}")

conn.close()
