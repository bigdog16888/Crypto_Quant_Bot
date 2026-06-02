import sqlite3

conn = sqlite3.connect('crypto_bot.db')

print("=== QUERY 1: All bots on normalized_pair = 'ETHUSDC' ===")
rows = conn.execute("""
    SELECT b.id, b.name, b.direction, b.is_active, b.status,
           t.open_qty, t.total_invested, t.cycle_id, t.entry_confirmed
    FROM bots b
    JOIN trades t ON t.bot_id = b.id
    WHERE b.normalized_pair = 'ETHUSDC'
    ORDER BY b.id
""").fetchall()

print(f"  {'id':>7}  {'name':<20}  {'dir':<6}  {'is_active':>9}  {'status':<24}  {'open_qty':>10}  {'total_invested':>15}  {'cycle_id':>8}  {'entry_confirmed':>15}")
print("  " + "-" * 140)
for r in rows:
    print(f"  {r[0]:>7}  {str(r[1]):<20}  {str(r[2]):<6}  {str(r[3]):>9}  {str(r[4]):<24}  {str(r[5]):>10}  {str(r[6]):>15}  {str(r[7]):>8}  {str(r[8]):>15}")

print()
print("=== QUERY 2: active_positions WHERE pair = 'ETHUSDC' ===")
pos = conn.execute("""
    SELECT pair, side, size, entry_price, bot_id
    FROM active_positions
    WHERE pair = 'ETHUSDC'
""").fetchall()

if pos:
    print(f"  {'pair':<12}  {'side':<6}  {'size':>10}  {'entry_price':>12}  {'bot_id':>8}")
    print("  " + "-" * 60)
    for r in pos:
        print(f"  {str(r[0]):<12}  {str(r[1]):<6}  {str(r[2]):>10}  {str(r[3]):>12}  {str(r[4]):>8}")
else:
    print("  NO ROWS")

conn.close()
