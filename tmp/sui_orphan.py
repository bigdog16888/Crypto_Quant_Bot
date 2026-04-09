import sqlite3

c = sqlite3.connect('crypto_bot.db')
q = c.cursor()

print("=== All active bots and their pairs ===")
q.execute("SELECT id, name, pair, direction, is_active FROM bots ORDER BY id")
for r in q.fetchall():
    print(f"  {r}")

print()
print("=== active_positions with bot_id=0 (orphaned) ===")
q.execute("SELECT pair, side, size, entry_price FROM active_positions WHERE bot_id=0")
for r in q.fetchall():
    print(f"  {r}")

print()
print("=== trades rows with no corresponding exchange position ===")
q.execute("SELECT t.bot_id, b.name, b.pair, t.total_invested, t.cycle_id FROM trades t JOIN bots b ON t.bot_id=b.id WHERE t.total_invested > 0")
for r in q.fetchall():
    print(f"  {r}")

print()
print("=== Any SUI-related bot_orders? ===")
q.execute("""
    SELECT bo.bot_id, b.name, b.pair, bo.order_type, bo.status, bo.filled_amount
    FROM bot_orders bo 
    LEFT JOIN bots b ON bo.bot_id=b.id
    WHERE (b.pair LIKE '%SUI%' OR bo.client_order_id LIKE '%SUI%')
    AND bo.filled_amount > 0
    ORDER BY bo.created_at DESC LIMIT 10
""")
for r in q.fetchall():
    print(f"  {r}")

c.close()
