import sqlite3, time

conn = sqlite3.connect('crypto_bot.db')

print("=== QUERY 1: short eth (100002) bot_orders with filled_amount > 0, ASC ===")
rows = conn.execute("""
    SELECT order_type, status, filled_amount, price, cycle_id,
           datetime(created_at, 'unixepoch') as created_at,
           client_order_id
    FROM bot_orders
    WHERE bot_id = 100002
      AND filled_amount > 0
    ORDER BY created_at ASC
""").fetchall()

# Compute running ledger using canonical entry/exit sets
ENTRIES = {'entry','grid','adoption','adoption_add','carry'}
EXITS   = {'tp','close','exit','adoption_reduce','dust_close','sl','virtual_netting'}

running = 0.0
total_entries = 0.0
total_exits   = 0.0

print(f"  {'order_type':<18}  {'status':<12}  {'filled':>8}  {'price':>10}  {'cycle_id':>8}  {'created_at':<20}  client_order_id")
print("  " + "-" * 130)
for r in rows:
    otype, status, filled, price, cycle_id, created, cid = r
    if otype in ENTRIES:
        running += filled
        total_entries += filled
        direction = 'ENTRY +'
    elif otype in EXITS:
        running -= filled
        total_exits += filled
        direction = 'EXIT  -'
    else:
        direction = f'OTHER  ({otype})'
    print(f"  {otype:<18}  {status:<12}  {filled:>8.4f}  {price:>10.4f}  {str(cycle_id):>8}  {created:<20}  {cid}")

print()
print(f"  Total ENTRY fills  : {total_entries:.6f}")
print(f"  Total EXIT  fills  : {total_exits:.6f}")
print(f"  Running net        : {running:.6f}  (positive = open LONG, negative = open SHORT)")
print(f"  Signed for SHORT   : {-running:.6f}  (SHORT bot: net should be negative = bot is short)")

print()
print("=== QUERY 2: short eth (100002) trades row ===")
t = conn.execute("""
    SELECT open_qty, total_invested, avg_entry_price, cycle_id, entry_confirmed
    FROM trades WHERE bot_id = 100002
""").fetchone()
if t:
    print(f"  open_qty={t[0]}  total_invested={t[1]}  avg_entry_price={t[2]}")
    print(f"  cycle_id={t[3]}  entry_confirmed={t[4]}")
else:
    print("  NO TRADES ROW")

conn.close()
